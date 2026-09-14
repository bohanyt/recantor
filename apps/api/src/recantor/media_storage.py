from __future__ import annotations

import errno
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


@dataclass(frozen=True)
class PreparedNormalizedMedia:
    session_id: UUID
    temp_path: Path
    manifest_temp_path: Path
    identity: NormalizedMediaIdentity


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
    """Crash-safe, immutable first-wins normalized evidence.

    Expensive candidate inspection/digest/fsync is done in ``prepare_temp`` while the WAV and
    manifest are still private. Final publication uses no-replace hard links. The stable manifest
    is the single authoritative commit marker and points to a content-addressed WAV object, so two
    publishers can never replace a committed winner or create a manifest/WAV pair from different
    candidates. Callers that need claim fencing must hold their durable claim fence while invoking
    ``install_prepared_first_wins``; this method intentionally performs only the bounded final
    filesystem install, not whole-file hashing.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def directory_for(self, session_id: UUID) -> Path:
        return self.root / "sessions" / str(session_id) / "normalized"

    def objects_directory_for(self, session_id: UUID) -> Path:
        return self.directory_for(session_id) / "objects"

    def object_path_for(self, session_id: UUID, normalized_sha256: str) -> Path:
        return self.objects_directory_for(session_id) / f"{normalized_sha256}.wav"

    def final_path_for(self, session_id: UUID) -> Path:
        """Return the committed object path, or the legacy orphan slot before commitment.

        The pre-manifest fallback preserves the historical crash-test concept that an orphan WAV
        without a manifest is non-authoritative. New publication never uses this mutable-looking
        slot; committed evidence always lives in the content-addressed objects directory.
        """
        identity = self._load_identity(session_id)
        if identity is not None:
            return self.object_path_for(session_id, identity.normalized_sha256)
        return self.directory_for(session_id) / f"{NORMALIZATION_SPEC_ID}.wav"

    def manifest_path_for(self, session_id: UUID) -> Path:
        return self.directory_for(session_id) / f"{NORMALIZATION_SPEC_ID}.json"

    def private_temp_path(self, session_id: UUID) -> Path:
        directory = self.directory_for(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f".{NORMALIZATION_SPEC_ID}.{uuid4().hex}.tmp.wav"

    def _private_manifest_path(self, session_id: UUID) -> Path:
        directory = self.directory_for(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f".{NORMALIZATION_SPEC_ID}.{uuid4().hex}.tmp.json"

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

    @staticmethod
    def _valid_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    def _validate_identity_metadata(
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
        if not self._valid_sha256(identity.normalized_sha256):
            raise NormalizedMediaError("normalized manifest hash identity is malformed")
        if identity.normalized_byte_length <= 0 or identity.total_samples < 1:
            raise NormalizedMediaError("normalized manifest payload identity is malformed")

        object_path = self.object_path_for(session_id, identity.normalized_sha256)
        return NormalizedMedia(
            key=self._key_for(object_path),
            sha256=identity.normalized_sha256,
            selected_audio_stream=identity.selected_audio_stream,
            byte_length=identity.normalized_byte_length,
            total_samples=identity.total_samples,
        )

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
        media = self._validate_identity_metadata(
            session_id=session_id,
            identity=identity,
            source_storage_key=source_storage_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            selected_audio_stream=selected_audio_stream,
        )
        object_path = self.object_path_for(session_id, identity.normalized_sha256)
        if not object_path.is_file() or object_path.is_symlink():
            raise NormalizedMediaError("committed normalized WAV is missing")
        actual_samples = self.inspect_wav(object_path)
        actual_hash, actual_length = _digest_file(object_path)
        if (
            actual_hash != identity.normalized_sha256
            or actual_length != identity.normalized_byte_length
            or actual_samples != identity.total_samples
        ):
            raise NormalizedMediaError("committed normalized WAV failed durable verification")
        return media

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

    def prepare_temp(
        self,
        *,
        session_id: UUID,
        source_storage_key: str,
        source_sha256: str,
        source_byte_length: int,
        selected_audio_stream: int,
        temp_path: Path,
    ) -> PreparedNormalizedMedia:
        """Digest and fsync a private candidate without creating authoritative evidence."""
        directory = self.directory_for(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        if not temp_path.is_file() or temp_path.is_symlink():
            raise NormalizedMediaError("normalized candidate is not a regular file")

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
        with temp_path.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())

        manifest_temp = self._private_manifest_path(session_id)
        try:
            with manifest_temp.open("xb") as handle:
                handle.write(self._identity_bytes(identity))
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with suppress(OSError):
                manifest_temp.unlink(missing_ok=True)
            raise
        return PreparedNormalizedMedia(
            session_id=session_id,
            temp_path=temp_path,
            manifest_temp_path=manifest_temp,
            identity=identity,
        )

    @staticmethod
    def discard_prepared(prepared: PreparedNormalizedMedia) -> None:
        with suppress(OSError):
            prepared.temp_path.unlink(missing_ok=True)
        with suppress(OSError):
            prepared.manifest_temp_path.unlink(missing_ok=True)

    def install_prepared_first_wins(
        self,
        prepared: PreparedNormalizedMedia,
    ) -> NormalizedMedia:
        """Atomically install the first immutable manifest/object pair without replacement.

        Correct claim fencing belongs to the caller: the caller must keep its PostgreSQL claim row
        locked and current for this bounded final operation. The stable manifest link is the commit
        point. Because both destination paths are installed with ``link(2)`` and never replaced,
        a loser can only observe and return the existing winner.
        """
        session_id = prepared.session_id
        identity = prepared.identity
        directory = self.directory_for(session_id)
        objects_directory = self.objects_directory_for(session_id)
        manifest_path = self.manifest_path_for(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        objects_directory.mkdir(parents=True, exist_ok=True)

        self._validate_identity_metadata(
            session_id=session_id,
            identity=identity,
            source_storage_key=identity.source_storage_key,
            source_sha256=identity.source_sha256,
            source_byte_length=identity.source_byte_length,
            selected_audio_stream=identity.selected_audio_stream,
        )

        object_path = self.object_path_for(session_id, identity.normalized_sha256)
        object_created = False
        try:
            try:
                os.link(prepared.temp_path, object_path)
                object_created = True
                _fsync_directory(objects_directory)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            if not object_path.is_file() or object_path.is_symlink():
                raise NormalizedMediaConflict("normalized object destination is not a regular file")
            if object_path.stat().st_size != identity.normalized_byte_length:
                raise NormalizedMediaConflict("normalized object hash slot has conflicting length")

            try:
                os.link(prepared.manifest_temp_path, manifest_path)
                _fsync_directory(directory)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise

            winner = self._load_identity(session_id)
            if winner is None:
                raise NormalizedMediaError("normalized manifest publication did not commit")
            media = self._validate_identity_metadata(
                session_id=session_id,
                identity=winner,
                source_storage_key=identity.source_storage_key,
                source_sha256=identity.source_sha256,
                source_byte_length=identity.source_byte_length,
                selected_audio_stream=identity.selected_audio_stream,
            )
            if object_created and winner.normalized_sha256 != identity.normalized_sha256:
                with suppress(OSError):
                    object_path.unlink(missing_ok=True)
                _fsync_directory(objects_directory)
            return media
        finally:
            self.discard_prepared(prepared)

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
        """Compatibility helper for non-claimed callers and storage-only tests.

        Media processing uses ``prepare_temp`` followed by a PostgreSQL-fenced call to
        ``install_prepared_first_wins``. This helper still has atomic filesystem first-wins
        semantics, but it does not by itself provide a database claim fence.
        """
        prepared = self.prepare_temp(
            session_id=session_id,
            source_storage_key=source_storage_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            selected_audio_stream=selected_audio_stream,
            temp_path=temp_path,
        )
        self.install_prepared_first_wins(prepared)
        return self.verify_committed(
            session_id=session_id,
            source_storage_key=source_storage_key,
            source_sha256=source_sha256,
            source_byte_length=source_byte_length,
            selected_audio_stream=selected_audio_stream,
        )

    def normalized_path(self, session_id: UUID, storage_key: str) -> Path:
        raw = self.root / storage_key
        if raw.is_symlink():
            raise NormalizedMediaError("normalized storage key resolves through a symlink")
        name = raw.name
        if not name.endswith(".wav"):
            raise NormalizedMediaError("normalized storage key is not a WAV object")
        normalized_sha256 = name[:-4]
        if not self._valid_sha256(normalized_sha256):
            raise NormalizedMediaError("normalized storage key has malformed content identity")
        expected = self.object_path_for(session_id, normalized_sha256)
        if raw != expected:
            raise NormalizedMediaError("normalized storage key does not match session identity")
        if not expected.is_file() or expected.is_symlink():
            raise NormalizedMediaError("normalized storage object is missing")
        return expected
