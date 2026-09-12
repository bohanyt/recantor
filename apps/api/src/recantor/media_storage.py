from __future__ import annotations

import hashlib
import json
import os
import wave
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from recantor.media_spec import (
    NORMALIZATION_SPEC_ID,
    UPLOAD_CHANNELS,
    UPLOAD_SAMPLE_RATE,
    UPLOAD_SAMPLE_WIDTH_BYTES,
)


class NormalizedMediaError(RuntimeError):
    pass


class NormalizedMediaConflict(NormalizedMediaError):
    pass


@dataclass(frozen=True)
class NormalizedMediaIdentity:
    session_id: str
    source_storage_key: str
    source_sha256: str
    source_byte_length: int
    normalization_spec_id: str
    selected_audio_stream: int
    normalized_sha256: str
    normalized_byte_length: int
    total_samples: int
    sample_rate: int
    channels: int
    sample_width: int


@dataclass(frozen=True)
class NormalizedMedia:
    key: str
    sha256: str
    selected_audio_stream: int
    byte_length: int
    total_samples: int


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class FilesystemNormalizedMediaStorage:
    """Crash-safe normalized evidence stored beside the canonical audio volume.

    The JSON manifest is the first durable identity evidence. A payload without a manifest is
    only an orphaned candidate and may be replaced by a deterministic retry. Once the manifest
    exists, its exact source/normalization identity is immutable and the WAV must continue to
    verify against it; a missing or mutated committed WAV fails loudly rather than being healed
    by silently replacing evidence.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def directory_for(self, session_id: UUID) -> Path:
        return self.root / "sessions" / str(session_id) / "normalized"

    def final_path_for(self, session_id: UUID) -> Path:
        return self.directory_for(session_id) / f"{NORMALIZATION_SPEC_ID}.wav"

    def manifest_path_for(self, session_id: UUID) -> Path:
        return self.directory_for(session_id) / f"{NORMALIZATION_SPEC_ID}.json"

    def private_temp_path(self, session_id: UUID) -> Path:
        directory = self.directory_for(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f".{NORMALIZATION_SPEC_ID}.{uuid4().hex}.tmp.wav"

    def _key_for(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def inspect_wav(path: Path) -> int:
        try:
            with wave.open(str(path), "rb") as source:
                if source.getnchannels() != UPLOAD_CHANNELS:
                    raise NormalizedMediaError("normalized WAV is not mono")
                if source.getsampwidth() != UPLOAD_SAMPLE_WIDTH_BYTES:
                    raise NormalizedMediaError("normalized WAV is not signed 16-bit PCM")
                if source.getframerate() != UPLOAD_SAMPLE_RATE:
                    raise NormalizedMediaError("normalized WAV is not 16 kHz")
                if source.getcomptype() != "NONE":
                    raise NormalizedMediaError("normalized WAV is not uncompressed PCM")
                frames = source.getnframes()
        except (OSError, EOFError, wave.Error) as exc:
            raise NormalizedMediaError("normalized WAV is unreadable") from exc
        if frames < 1:
            raise NormalizedMediaError("normalized WAV contains no audio samples")
        return frames

    @staticmethod
    def _identity_bytes(identity: NormalizedMediaIdentity) -> bytes:
        return json.dumps(
            asdict(identity),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _load_identity(self, session_id: UUID) -> NormalizedMediaIdentity | None:
        path = self.manifest_path_for(session_id)
        if not path.exists():
            return None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            return NormalizedMediaIdentity(**parsed)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise NormalizedMediaError("normalized media manifest is unreadable") from exc

    def _verify_identity(
        self,
        *,
        session_id: UUID,
        identity: NormalizedMediaIdentity,
        source_storage_key: str,
        source_sha256: str,
        source_byte_length: int,
        selected_audio_stream: int | None = None,
    ) -> NormalizedMedia:
        if identity.session_id != str(session_id):
            raise NormalizedMediaConflict("normalized manifest session identity drifted")
        if identity.source_storage_key != source_storage_key:
            raise NormalizedMediaConflict("normalized manifest source storage identity drifted")
        if identity.source_sha256 != source_sha256:
            raise NormalizedMediaConflict("normalized manifest source hash identity drifted")
        if identity.source_byte_length != source_byte_length:
            raise NormalizedMediaConflict("normalized manifest source length identity drifted")
        if identity.normalization_spec_id != NORMALIZATION_SPEC_ID:
            raise NormalizedMediaConflict("normalized manifest specification identity drifted")
        if (
            selected_audio_stream is not None
            and identity.selected_audio_stream != selected_audio_stream
        ):
            raise NormalizedMediaConflict(
                "normalized manifest selected audio stream identity drifted"
            )
        if (
            identity.sample_rate != UPLOAD_SAMPLE_RATE
            or identity.channels != UPLOAD_CHANNELS
            or identity.sample_width != UPLOAD_SAMPLE_WIDTH_BYTES
        ):
            raise NormalizedMediaConflict("normalized manifest PCM format identity drifted")

        final_path = self.final_path_for(session_id)
        if not final_path.is_file() or final_path.is_symlink():
            raise NormalizedMediaError("committed normalized WAV is missing")
        actual_samples = self.inspect_wav(final_path)
        actual_hash, actual_length = _digest_file(final_path)
        if (
            actual_hash != identity.normalized_sha256
            or actual_length != identity.normalized_byte_length
            or actual_samples != identity.total_samples
        ):
            raise NormalizedMediaError("committed normalized WAV failed durable verification")
        return NormalizedMedia(
            key=self._key_for(final_path),
            sha256=actual_hash,
            selected_audio_stream=identity.selected_audio_stream,
            byte_length=actual_length,
            total_samples=actual_samples,
        )

    def verify_committed(
        self,
        *,
        session_id: UUID,
        source_storage_key: str,
        source_sha256: str,
        source_byte_length: int,
        selected_audio_stream: int | None = None,
    ) -> NormalizedMedia:
        identity = self._load_identity(session_id)
        if identity is None:
            raise NormalizedMediaError("normalized media manifest is missing")
        return self._verify_identity(
            session_id=session_id,
            identity=identity,
            source_storage_key=source_storage_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            selected_audio_stream=selected_audio_stream,
        )

    def publish_temp(
        self,
        *,
        session_id: UUID,
        source_storage_key: str,
        source_sha256: str,
        source_byte_length: int,
        selected_audio_stream: int,
        temp_path: Path,
    ) -> NormalizedMedia:
        directory = self.directory_for(session_id)
        final_path = self.final_path_for(session_id)
        manifest_path = self.manifest_path_for(session_id)
        directory.mkdir(parents=True, exist_ok=True)

        existing_identity = self._load_identity(session_id)
        if existing_identity is not None:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
            return self._verify_identity(
                session_id=session_id,
                identity=existing_identity,
                source_storage_key=source_storage_key,
                source_sha256=source_sha256,
                source_byte_length=source_byte_length,
                selected_audio_stream=selected_audio_stream,
            )

        total_samples = self.inspect_wav(temp_path)
        normalized_sha256, normalized_byte_length = _digest_file(temp_path)
        identity = NormalizedMediaIdentity(
            session_id=str(session_id),
            source_storage_key=source_storage_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            normalization_spec_id=NORMALIZATION_SPEC_ID,
            selected_audio_stream=selected_audio_stream,
            normalized_sha256=normalized_sha256,
            normalized_byte_length=normalized_byte_length,
            total_samples=total_samples,
            sample_rate=UPLOAD_SAMPLE_RATE,
            channels=UPLOAD_CHANNELS,
            sample_width=UPLOAD_SAMPLE_WIDTH_BYTES,
        )

        # A final WAV without a manifest is an orphan from an interrupted first publish. It is
        # not authoritative and deterministic retry may replace it. Once the manifest exists,
        # the early return above turns any later mismatch into an error instead.
        try:
            with temp_path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, final_path)
            _fsync_directory(directory)

            manifest_temp = directory / f".{manifest_path.name}.{uuid4().hex}.tmp"
            try:
                with manifest_temp.open("xb") as handle:
                    handle.write(self._identity_bytes(identity))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(manifest_temp, manifest_path)
                _fsync_directory(directory)
            finally:
                with suppress(OSError):
                    manifest_temp.unlink(missing_ok=True)
        finally:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)

        return self._verify_identity(
            session_id=session_id,
            identity=identity,
            source_storage_key=source_storage_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            selected_audio_stream=selected_audio_stream,
        )

    def normalized_path(self, session_id: UUID, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        expected = self.final_path_for(session_id).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise NormalizedMediaError("normalized storage key escapes audio root") from exc
        if candidate != expected:
            raise NormalizedMediaError("normalized storage key does not match stable identity")
        return candidate
