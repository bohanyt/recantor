import hashlib
from uuid import uuid4

import pytest

from recantor.storage import AudioStorageConflict, FilesystemAudioStorage


def test_filesystem_storage_commits_and_reuses_identical_orphan(tmp_path) -> None:
    storage = FilesystemAudioStorage(tmp_path)
    payload = b"recantor-audio-fragment"
    digest = hashlib.sha256(payload).hexdigest()
    session_id = uuid4()

    first = storage.commit_bytes(
        session_id=session_id,
        sequence=1,
        content_type="audio/webm;codecs=opus",
        payload=payload,
        sha256=digest,
    )
    second = storage.commit_bytes(
        session_id=session_id,
        sequence=1,
        content_type="audio/webm;codecs=opus",
        payload=payload,
        sha256=digest,
    )

    assert first == second
    assert storage.verify(first.key, sha256=digest, byte_length=len(payload))
    assert (tmp_path / first.key).read_bytes() == payload


def test_filesystem_storage_rejects_conflicting_existing_sequence(tmp_path) -> None:
    storage = FilesystemAudioStorage(tmp_path)
    session_id = uuid4()
    first = b"first"
    second = b"second"

    storage.commit_bytes(
        session_id=session_id,
        sequence=4,
        content_type="audio/webm",
        payload=first,
        sha256=hashlib.sha256(first).hexdigest(),
    )

    with pytest.raises(AudioStorageConflict):
        storage.commit_bytes(
            session_id=session_id,
            sequence=4,
            content_type="audio/webm",
            payload=second,
            sha256=hashlib.sha256(second).hexdigest(),
        )
