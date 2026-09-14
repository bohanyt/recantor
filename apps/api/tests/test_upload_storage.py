from __future__ import annotations

import hashlib

import pytest

from recantor.upload_storage import FilesystemUploadStorage, UploadStorageError


def test_completed_upload_has_stable_recantor_owned_identity(tmp_path):
    payload = b"immutable-upload-source" * 64
    storage = FilesystemUploadStorage(tmp_path, "uploads/tus")
    storage.upload_root.mkdir(parents=True)
    upload_id = "0123456789abcdef0123456789abcdef"
    (storage.upload_root / upload_id).write_bytes(payload)

    completed = storage.inspect_completed(upload_id, expected_bytes=len(payload))

    assert completed.key == f"uploads/tus/{upload_id}"
    assert completed.byte_length == len(payload)
    assert completed.sha256 == hashlib.sha256(payload).hexdigest()


def test_completed_upload_rejects_unsafe_or_mismatched_storage_identity(tmp_path):
    storage = FilesystemUploadStorage(tmp_path, "uploads/tus")
    storage.upload_root.mkdir(parents=True)

    with pytest.raises(UploadStorageError, match="safe"):
        storage.inspect_completed("../escape", expected_bytes=1)

    upload_id = "fedcba9876543210fedcba9876543210"
    (storage.upload_root / upload_id).write_bytes(b"abc")
    with pytest.raises(UploadStorageError, match="length"):
        storage.inspect_completed(upload_id, expected_bytes=4)
