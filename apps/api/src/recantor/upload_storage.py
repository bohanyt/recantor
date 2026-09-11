from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TUS_UPLOAD_ID = re.compile(r"^[A-Za-z0-9._~-]{8,160}$")


class UploadStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    key: str
    byte_length: int


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

    def inspect_completed(self, upload_id: str, *, expected_bytes: int) -> StoredUpload:
        if _TUS_UPLOAD_ID.fullmatch(upload_id) is None:
            raise UploadStorageError("tus upload id is not safe for local storage lookup")

        path = (self.upload_root / upload_id).resolve()
        try:
            path.relative_to(self.upload_root)
        except ValueError as exc:
            raise UploadStorageError("tus upload path escapes the upload root") from exc

        try:
            stat = path.stat()
        except OSError as exc:
            raise UploadStorageError("completed tus upload is missing or unreadable") from exc
        if not path.is_file():
            raise UploadStorageError("completed tus upload is not a regular file")
        if stat.st_size != expected_bytes:
            raise UploadStorageError("completed tus upload length does not match declared length")

        return StoredUpload(
            key=path.relative_to(self.root).as_posix(),
            byte_length=stat.st_size,
        )
