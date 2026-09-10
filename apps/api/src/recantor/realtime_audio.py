from __future__ import annotations

import io
import math
import struct
import wave
from collections import deque
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RealtimeAudioProtocolError(RuntimeError):
    pass


class RealtimeAudioStart(BaseModel):
    type: Literal["start"]
    writer_id: str = Field(min_length=8, max_length=128)
    capture_epoch: int = Field(ge=1)
    stream_id: UUID
    timeline_base_ms: int = Field(ge=0)
    sample_rate: int = Field(ge=8_000, le=96_000)
    channels: Literal[1]
    sample_format: Literal["s16le"]


@dataclass(frozen=True)
class VadConfig:
    pre_roll_ms: int = 200
    min_voiced_ms: int = 160
    silence_ms: int = 600
    hard_max_ms: int = 8_000
    absolute_threshold_dbfs: float = -50.0
    noise_margin_db: float = 12.0
    initial_noise_dbfs: float = -65.0
    noise_alpha: float = 0.95


@dataclass(frozen=True)
class PcmFrame:
    sample_offset: int
    pcm: bytes

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // 2

    @property
    def end_sample(self) -> int:
        return self.sample_offset + self.sample_count


@dataclass(frozen=True)
class UtteranceCandidate:
    start_sample: int
    end_sample: int
    pcm: bytes
    voiced_samples: int


HEADER_BYTES = 8
MAX_PCM_PACKET_BYTES = 64 * 1024


def parse_pcm_packet(payload: bytes, *, max_bytes: int = MAX_PCM_PACKET_BYTES) -> PcmFrame:
    if len(payload) < HEADER_BYTES + 2:
        raise RealtimeAudioProtocolError("PCM packet is too short")
    if len(payload) > max_bytes:
        raise RealtimeAudioProtocolError("PCM packet exceeds realtime size limit")
    pcm = payload[HEADER_BYTES:]
    if len(pcm) % 2:
        raise RealtimeAudioProtocolError("s16le PCM packet must contain whole samples")
    sample_offset = struct.unpack_from("<Q", payload, 0)[0]
    return PcmFrame(sample_offset=sample_offset, pcm=pcm)


def pcm_dbfs(pcm: bytes) -> float:
    if not pcm or len(pcm) % 2:
        raise RealtimeAudioProtocolError("invalid s16le PCM payload")
    sample_count = len(pcm) // 2
    square_sum = 0.0
    for (sample,) in struct.iter_unpack("<h", pcm):
        square_sum += float(sample) * float(sample)
    rms = math.sqrt(square_sum / sample_count)
    if rms <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms / 32768.0))


def encode_pcm_wav(pcm: bytes, *, sample_rate: int) -> bytes:
    if sample_rate < 1:
        raise RealtimeAudioProtocolError("sample rate must be positive")
    if not pcm or len(pcm) % 2:
        raise RealtimeAudioProtocolError("WAV source must be non-empty s16le PCM")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return buffer.getvalue()


def sample_to_timeline_ms(timeline_base_ms: int, sample_offset: int, sample_rate: int) -> int:
    if timeline_base_ms < 0 or sample_offset < 0 or sample_rate < 1:
        raise RealtimeAudioProtocolError("invalid realtime timeline input")
    return timeline_base_ms + round(sample_offset * 1_000 / sample_rate)


class EnergyEndpointDetector:
    """Small deterministic VAD baseline; thresholds are tuning defaults, not product guarantees."""

    def __init__(self, sample_rate: int, config: VadConfig | None = None):
        if sample_rate < 8_000 or sample_rate > 96_000:
            raise RealtimeAudioProtocolError("unsupported realtime sample rate")
        self.sample_rate = sample_rate
        self.config = config or VadConfig()
        self.pre_roll_limit = max(0, round(sample_rate * self.config.pre_roll_ms / 1_000))
        self.min_voiced_samples = max(1, round(sample_rate * self.config.min_voiced_ms / 1_000))
        self.silence_samples = max(1, round(sample_rate * self.config.silence_ms / 1_000))
        self.hard_max_samples = max(1, round(sample_rate * self.config.hard_max_ms / 1_000))
        self.noise_dbfs = self.config.initial_noise_dbfs
        self._pre_roll: deque[PcmFrame] = deque()
        self._pre_roll_samples = 0
        self._active: list[PcmFrame] = []
        self._active_start: int | None = None
        self._voiced_samples = 0
        self._trailing_silence_samples = 0

    @property
    def active(self) -> bool:
        return self._active_start is not None

    def reset(self) -> None:
        self._pre_roll.clear()
        self._pre_roll_samples = 0
        self._active.clear()
        self._active_start = None
        self._voiced_samples = 0
        self._trailing_silence_samples = 0

    def _threshold_dbfs(self) -> float:
        return max(
            self.config.absolute_threshold_dbfs,
            self.noise_dbfs + self.config.noise_margin_db,
        )

    def _classify(self, frame: PcmFrame) -> bool:
        level = pcm_dbfs(frame.pcm)
        speech = level >= self._threshold_dbfs()
        if not speech:
            alpha = min(0.999, max(0.0, self.config.noise_alpha))
            self.noise_dbfs = alpha * self.noise_dbfs + (1.0 - alpha) * level
        return speech

    def _append_pre_roll(self, frame: PcmFrame) -> None:
        if self.pre_roll_limit <= 0:
            return
        self._pre_roll.append(frame)
        self._pre_roll_samples += frame.sample_count
        while self._pre_roll and self._pre_roll_samples > self.pre_roll_limit:
            removed = self._pre_roll.popleft()
            self._pre_roll_samples -= removed.sample_count

    def _begin(self, frame: PcmFrame) -> None:
        self._active = [*self._pre_roll, frame]
        self._active_start = self._active[0].sample_offset
        self._voiced_samples = frame.sample_count
        self._trailing_silence_samples = 0
        self._pre_roll.clear()
        self._pre_roll_samples = 0

    def _finish(self) -> UtteranceCandidate | None:
        if self._active_start is None or not self._active:
            self.reset()
            return None
        end_sample = self._active[-1].end_sample
        result = None
        if self._voiced_samples >= self.min_voiced_samples:
            result = UtteranceCandidate(
                start_sample=self._active_start,
                end_sample=end_sample,
                pcm=b"".join(frame.pcm for frame in self._active),
                voiced_samples=self._voiced_samples,
            )
        self.reset()
        return result

    def feed(self, frame: PcmFrame) -> list[UtteranceCandidate]:
        if frame.sample_count < 1:
            raise RealtimeAudioProtocolError("PCM frame must contain at least one sample")
        speech = self._classify(frame)
        if not self.active:
            if speech:
                self._begin(frame)
            else:
                self._append_pre_roll(frame)
            return []

        self._active.append(frame)
        if speech:
            self._voiced_samples += frame.sample_count
            self._trailing_silence_samples = 0
        else:
            self._trailing_silence_samples += frame.sample_count

        active_samples = self._active[-1].end_sample - (self._active_start or 0)
        hard_max = active_samples >= self.hard_max_samples
        endpoint = self._trailing_silence_samples >= self.silence_samples
        if not hard_max and not endpoint:
            return []

        candidate = self._finish()
        return [candidate] if candidate is not None else []

    def flush(self) -> UtteranceCandidate | None:
        return self._finish() if self.active else None
