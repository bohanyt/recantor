from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select

from recantor.db import get_sessionmaker
from recantor.models import TranscriptionUtterance, TranscriptSegment
from recantor.settings import get_settings
from recantor.storage import AudioStorageError, FilesystemAudioStorage
from recantor.transcript import commit_transcript_segment
from recantor.utterance import transcript_producer_key_for_utterance


class STTError(RuntimeError):
    pass


class STTSourceNotFound(STTError):
    pass


class STTSourceError(STTError):
    pass


class STTErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"


class STTProviderError(STTError):
    def __init__(self, category: STTErrorCategory, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class STTRequest:
    audio: bytes
    filename: str
    content_type: str
    language: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class STTResult:
    text: str
    language: str | None = None


class STTProvider(Protocol):
    async def transcribe(self, request: STTRequest) -> STTResult: ...


_EXTENSION_BY_CONTENT_TYPE = {
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
}
_MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024


def _filename_for_content_type(content_type: str) -> str:
    bare = content_type.split(";", 1)[0].strip().lower()
    return f"utterance{_EXTENSION_BY_CONTENT_TYPE.get(bare, '.bin')}"


def _multipart_body(
    *,
    boundary: str,
    fields: list[tuple[str, str]],
    filename: str,
    content_type: str,
    audio: bytes,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_filename = filename.replace('"', "")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            audio,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks)


class GroqSTTProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        endpoint: str,
        model: str,
        timeout_seconds: float,
    ):
        self.api_key = (api_key or "").strip()
        self.endpoint = endpoint.strip()
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls) -> GroqSTTProvider:
        settings = get_settings()
        return cls(
            api_key=settings.groq_api_key,
            endpoint=settings.groq_stt_endpoint,
            model=settings.groq_stt_model,
            timeout_seconds=settings.groq_stt_timeout_seconds,
        )

    def _validate_configuration(self) -> None:
        if not self.api_key:
            raise STTProviderError(
                STTErrorCategory.CONFIGURATION,
                "Groq STT API key is not configured",
            )
        if not self.endpoint.startswith(("https://", "http://")):
            raise STTProviderError(
                STTErrorCategory.CONFIGURATION,
                "Groq STT endpoint must be an HTTP(S) URL",
            )
        if not self.model:
            raise STTProviderError(
                STTErrorCategory.CONFIGURATION,
                "Groq STT model is not configured",
            )
        if self.timeout_seconds <= 0:
            raise STTProviderError(
                STTErrorCategory.CONFIGURATION,
                "Groq STT timeout must be positive",
            )

    def _request_sync(self, request: STTRequest) -> bytes:
        boundary = f"recantor-{uuid4().hex}"
        fields = [
            ("model", self.model),
            ("response_format", "json"),
            ("temperature", "0"),
        ]
        if request.language and request.language.strip():
            fields.append(("language", request.language.strip().lower()))
        if request.prompt and request.prompt.strip():
            fields.append(("prompt", request.prompt.strip()))
        body = _multipart_body(
            boundary=boundary,
            fields=fields,
            filename=request.filename,
            content_type=request.content_type,
            audio=request.audio,
        )
        http_request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                payload = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                category = STTErrorCategory.RATE_LIMIT
            elif exc.code in {401, 403}:
                category = STTErrorCategory.CONFIGURATION
            elif exc.code in {408, 425} or exc.code >= 500:
                category = STTErrorCategory.TRANSIENT
            else:
                category = STTErrorCategory.PERMANENT
            raise STTProviderError(category, f"Groq STT returned HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise STTProviderError(STTErrorCategory.TIMEOUT, "Groq STT request timed out") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                category = STTErrorCategory.TIMEOUT
            else:
                category = STTErrorCategory.TRANSIENT
            raise STTProviderError(category, "Groq STT network request failed") from exc
        except OSError as exc:
            raise STTProviderError(STTErrorCategory.TRANSIENT, "Groq STT transport failed") from exc

        if len(payload) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise STTProviderError(
                STTErrorCategory.MALFORMED_RESPONSE,
                "Groq STT response exceeded size limit",
            )
        return payload

    async def transcribe(self, request: STTRequest) -> STTResult:
        self._validate_configuration()
        if not request.audio:
            raise STTProviderError(
                STTErrorCategory.PERMANENT,
                "STT request audio must not be empty",
            )
        payload = await asyncio.to_thread(self._request_sync, request)
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise STTProviderError(
                STTErrorCategory.MALFORMED_RESPONSE,
                "Groq STT returned invalid JSON",
            ) from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
            raise STTProviderError(
                STTErrorCategory.MALFORMED_RESPONSE,
                "Groq STT response did not contain text",
            )
        text = parsed["text"].strip()
        if not text:
            raise STTProviderError(
                STTErrorCategory.MALFORMED_RESPONSE,
                "Groq STT returned blank transcript text",
            )
        response_language = parsed.get("language")
        language = (
            response_language.strip().lower()
            if isinstance(response_language, str) and response_language.strip()
            else request.language.strip().lower()
            if request.language and request.language.strip()
            else None
        )
        return STTResult(text=text, language=language)


async def transcribe_utterance(
    *,
    session_id: UUID,
    work_id: UUID,
    provider: STTProvider,
    language: str | None = None,
    prompt: str | None = None,
) -> tuple[TranscriptSegment, bool]:
    transcript_key = transcript_producer_key_for_utterance(work_id)

    async with get_sessionmaker()() as db:
        work = await db.scalar(
            select(TranscriptionUtterance).where(
                TranscriptionUtterance.id == work_id,
                TranscriptionUtterance.session_id == session_id,
            )
        )
        existing = await db.scalar(
            select(TranscriptSegment).where(
                TranscriptSegment.session_id == session_id,
                TranscriptSegment.producer_key == transcript_key,
            )
        )

    if work is None:
        raise STTSourceNotFound("transcription utterance not found")
    if existing is not None:
        if existing.start_ms != work.start_ms or existing.end_ms != work.end_ms:
            raise STTSourceError("canonical transcript timing conflicts with utterance work")
        return existing, True

    storage = FilesystemAudioStorage(get_settings().audio_storage_path)
    try:
        audio = storage.read_utterance_bytes(
            session_id=session_id,
            work_id=work.id,
            producer_key=work.producer_key,
            start_ms=work.start_ms,
            end_ms=work.end_ms,
            content_type=work.content_type,
            storage_key=work.storage_key,
            sha256=work.sha256,
            byte_length=work.byte_length,
        )
    except AudioStorageError as exc:
        raise STTSourceError("durable utterance media failed verification") from exc

    result = await provider.transcribe(
        STTRequest(
            audio=audio,
            filename=_filename_for_content_type(work.content_type),
            content_type=work.content_type,
            language=language,
            prompt=prompt,
        )
    )
    text = result.text.strip()
    if not text:
        raise STTProviderError(
            STTErrorCategory.MALFORMED_RESPONSE,
            "STT provider returned blank transcript text",
        )
    canonical_language = (
        result.language.strip().lower()
        if result.language and result.language.strip()
        else None
    )

    async with get_sessionmaker()() as db:
        return await commit_transcript_segment(
            db,
            session_id=session_id,
            producer_key=transcript_key,
            start_ms=work.start_ms,
            end_ms=work.end_ms,
            text=text,
            language=canonical_language,
        )
