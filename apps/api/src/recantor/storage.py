from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4


class AudioStorageError(RuntimeError):
    pass


class AudioStorageConflict(AudioStorageError):
    pass


@dataclass(frozen=True)
class StoredAudio:
    key: str
    sha256: str
    byte_length: int


_EXTENSION_BY_CONTENT_TYPE = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


class FilesystemAudioStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def _extension(content_type: str) -> str:
        bare = content_type.split(";", 1)[0].strip().lower()
        return _EXTENSION_BY_CONTENT_TYPE.get(bare, ".bin")

    def _path_for(self, session_id: UUID, sequence: int, content_type: str) -> Path:
        extension = self._extension(content_type)
        return self.root / "sessions" / str(session_id) / "raw" / f"{sequence:08d}{extension}"

    def _key_for(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def _digest_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    @staticmethod
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

    def commit_bytes(
        self,
        *,
        session_id: UUID,
        sequence: int,
        content_type: str,
        payload: bytes,
        sha256: str,
    ) -> StoredAudio:
        final_path = self._path_for(session_id, sequence, content_type)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        if final_path.exists():
            existing_hash, existing_length = self._digest_file(final_path)
            if existing_hash != sha256 or existing_length != len(payload):
                raise AudioStorageConflict("existing storage object has different content")
            return StoredAudio(self._key_for(final_path), existing_hash, existing_length)

        temp_path = final_path.with_name(f".{final_path.name}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, final_path)
            self._fsync_directory(final_path.parent)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

        stored_hash, stored_length = self._digest_file(final_path)
        if stored_hash != sha256 or stored_length != len(payload):
            raise AudioStorageError("stored audio failed integrity verification")
        return StoredAudio(self._key_for(final_path), stored_hash, stored_length)

    def verify(self, key: str, *, sha256: str, byte_length: int) -> bool:
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        if not path.is_file():
            return False
        actual_hash, actual_length = self._digest_file(path)
        return actual_hash == sha256 and actual_length == byte_length
