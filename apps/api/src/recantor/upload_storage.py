from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_TUS_UPLOAD_ID = re.compile(r"^[A-Za-z0-9._~-]{8,160}$")
_HASH_CHUNK_BYTES = 1024 * 1024


class UploadStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    key: str
    byte_length: int
    sha256: str
    device: int = 0
    inode: int = 0
    mtime_ns: int = 0


class FilesystemUploadStorage:
    """Recantor-owned view of completed tusd objects on the shared audio volume."""

    def __init__(self, root: str | Path, prefix: str):
        self.root = Path(root).expanduser().resolve()
        prefix_path = Path(prefix)
        if prefix_path.is_absolute() or ".." in prefix_path.parts:
            raise ValueError("upload storage prefix must stay inside the audio root")
        self.prefix = prefix_path
        self.upload_root = (self.root / prefix_path).resolve()
        try:
            self.upload_root.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("upload storage prefix escapes the audio root") from exc

    def _path_for(self, upload_id: str) -> Path:
        if _TUS_UPLOAD_ID.fullmatch(upload_id) is None:
            raise UploadStorageError("tus upload id is not safe for local storage lookup")
        path = self.upload_root / upload_id
        if path.is_symlink():
            raise UploadStorageError("completed tus upload must not be a symlink")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.upload_root)
        except ValueError as exc:
            raise UploadStorageError("tus upload path escapes the upload root") from exc
        return resolved

    def inspect_completed(self, upload_id: str, *, expected_bytes: int) -> StoredUpload:
        path = self._path_for(upload_id)
        try:
            before = path.stat()
        except OSError as exc:
            raise UploadStorageError("completed tus upload is missing or unreadable") from exc
        if not path.is_file():
            raise UploadStorageError("completed tus upload is not a regular file")
        if before.st_size != expected_bytes:
            raise UploadStorageError("completed tus upload length does not match declared length")

        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                opened = source.fileno()
                open_before = os.fstat(opened)
                for chunk in iter(lambda: source.read(_HASH_CHUNK_BYTES), b""):
                    digest.update(chunk)
                open_after = os.fstat(opened)
        except OSError as exc:
            raise UploadStorageError("completed tus upload could not be hashed") from exc

        identity_before = (open_before.st_dev, open_before.st_ino, open_before.st_size, open_before.st_mtime_ns)
        identity_after = (open_after.st_dev, open_after.st_ino, open_after.st_size, open_after.st_mtime_ns)
        if identity_before != identity_after or identity_after[2] != expected_bytes:
            raise UploadStorageError("completed tus upload changed while it was being hashed")
        try:
            after = path.stat()
        except OSError as exc:
            raise UploadStorageError("completed tus upload disappeared after hashing") from exc
        path_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if path_identity != identity_after:
            raise UploadStorageError("completed tus upload binding changed after hashing")

        return StoredUpload(
            key=path.relative_to(self.root).as_posix(),
            byte_length=after.st_size,
            sha256=digest.hexdigest(),
            device=after.st_dev,
            inode=after.st_ino,
            mtime_ns=after.st_mtime_ns,
        )

    def revalidate_completed(self, upload_id: str, evidence: StoredUpload, *, expected_bytes: int) -> None:
        path = self._path_for(upload_id)
        expected_key = path.relative_to(self.root).as_posix()
        if evidence.key != expected_key or evidence.byte_length != expected_bytes:
            raise UploadStorageError("completed upload evidence does not match current binding")
        try:
            current = path.stat()
        except OSError as exc:
            raise UploadStorageError("completed tus upload is missing during publish") from exc
        current_identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        evidence_identity = (evidence.device, evidence.inode, evidence.byte_length, evidence.mtime_ns)
        if current_identity != evidence_identity:
            raise UploadStorageError("completed tus upload changed before durable publish")

    def completed_path(
        self,
        upload_id: str,
        *,
        expected_key: str,
        expected_bytes: int,
    ) -> Path:
        path = self._path_for(upload_id)
        if path.relative_to(self.root).as_posix() != expected_key:
            raise UploadStorageError("completed upload storage key does not match tus identity")
        try:
            stat = path.stat()
        except OSError as exc:
            raise UploadStorageError("completed upload source is missing") from exc
        if stat.st_size != expected_bytes or not path.is_file():
            raise UploadStorageError("completed upload source length changed")
        return path
