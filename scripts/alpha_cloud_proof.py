from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "recantor-alpha-proof",
    "-f",
    "infra/compose.yaml",
    "-f",
    "infra/compose.alpha-proof.yaml",
]
API = "http://127.0.0.1:8000/api/v1"


def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=ROOT,
        text=True,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def compose(
    *args: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return run(*COMPOSE, *args, check=check, capture=capture)


def http(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url, data=body, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def json_http(
    method: str,
    url: str,
    payload: object | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    request_headers = {"accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["content-type"] = "application/json"
    status, _, raw = http(method, url, body=data, headers=request_headers)
    assert 200 <= status < 300, (method, url, status, raw[:500])
    return json.loads(raw or b"{}")


def wait_http(url: str, attempts: int = 120) -> None:
    for _ in range(attempts):
        try:
            status, _, _ = http("GET", url, timeout=2)
        except urllib.error.URLError:
            time.sleep(1)
            continue
        if 200 <= status < 300:
            return
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {url}")


def wait_stack() -> None:
    wait_http("http://127.0.0.1:8000/readyz")
    wait_http("http://127.0.0.1:5173")


def make_wav(path: Path, seconds: float = 0.6) -> bytes:
    sample_rate = 16000
    frames = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            value = 9000 if (index // 80) % 2 == 0 else -9000
            payload.extend(int(value).to_bytes(2, "little", signed=True))
        wav.writeframes(bytes(payload))
    return path.read_bytes()


def create_live() -> str:
    result = json_http(
        "POST",
        f"{API}/sessions/live",
        {
            "client_request_id": str(uuid.uuid4()),
            "writer_id": "alpha-proof-writer",
            "recovery_token": "alpha-proof-recovery-token-00000001",
        },
    )
    return str(result["id"])


def seed_utterance(session_id: str, producer_key: str, wav_bytes: bytes) -> str:
    encoded = base64.b64encode(wav_bytes).decode()
    program = r"""
import asyncio, base64, os
from uuid import UUID
from recantor.db import get_sessionmaker
from recantor.utterance import commit_utterance_work

async def main():
    async with get_sessionmaker()() as db:
        work, _ = await commit_utterance_work(
            db,
            session_id=UUID(os.environ["SESSION_ID"]),
            producer_key=os.environ["PRODUCER_KEY"],
            start_ms=0,
            end_ms=600,
            content_type="audio/wav",
            payload=base64.b64decode(os.environ["AUDIO_B64"]),
        )
    print(work.id)
asyncio.run(main())
"""
    result = compose(
        "exec",
        "-T",
        "-e",
        f"SESSION_ID={session_id}",
        "-e",
        f"PRODUCER_KEY={producer_key}",
        "-e",
        f"AUDIO_B64={encoded}",
        "api",
        "python",
        "-c",
        program,
        capture=True,
    )
    work_id = result.stdout.strip().splitlines()[-1]
    assert work_id
    return work_id


def transcript(session_id: str) -> dict[str, object]:
    return json_http(
        "GET", f"{API}/sessions/{session_id}/transcript?after_sequence=0&limit=100"
    )


def wait_transcript(
    session_id: str, text: str = "alpha proof transcript", attempts: int = 90
) -> dict[str, object]:
    for _ in range(attempts):
        result = transcript(session_id)
        segments = result.get("segments") or []
        if any(
            isinstance(segment, dict) and segment.get("text") == text
            for segment in segments
        ):
            return result
        time.sleep(1)
    raise AssertionError(f"canonical transcript did not converge for {session_id}")


def create_archive_evidence(wav_bytes: bytes) -> tuple[str, str]:
    session_id = create_live()
    digest = hashlib.sha256(wav_bytes).hexdigest()
    query = urllib.parse.urlencode(
        {
            "writer_id": "alpha-proof-writer",
            "capture_epoch": 1,
            "monotonic_start_ms": 0,
            "monotonic_end_ms": 600,
            "sha256": digest,
            "content_type": "audio/wav",
        }
    )
    status, _, raw = http(
        "PUT",
        f"{API}/sessions/{session_id}/chunks/1?{query}",
        body=wav_bytes,
        headers={"content-type": "application/octet-stream"},
    )
    assert status == 200, (status, raw[:500])
    ack = json.loads(raw)
    return session_id, str(ack["storage_key"])


def tus_metadata(values: dict[str, str]) -> str:
    return ",".join(
        f"{key} {base64.b64encode(value.encode()).decode()}"
        for key, value in values.items()
    )


def create_upload_and_wait(wav_bytes: bytes) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    created = json_http(
        "POST",
        f"{API}/uploads",
        {
            "client_request_id": str(uuid.uuid4()),
            "capability_token": token,
            "original_filename": "alpha-proof.wav",
            "content_type": "audio/wav",
            "byte_length": len(wav_bytes),
            "duration_ms": 600,
        },
    )
    session_id = str(created["session_id"])
    headers = {
        "Tus-Resumable": "1.0.0",
        "Upload-Length": str(len(wav_bytes)),
        "Upload-Metadata": tus_metadata(
            {
                "recantor_session_id": session_id,
                "filename": "alpha-proof.wav",
                "filetype": "audio/wav",
            }
        ),
        "X-Recantor-Upload-Token": token,
    }
    status, response_headers, raw = http(
        "POST", "http://127.0.0.1:1080/files/", headers=headers
    )
    assert status == 201, (status, raw[:500])
    location = response_headers.get("Location") or response_headers.get("location")
    assert location
    upload_url = urllib.parse.urljoin(
        "http://127.0.0.1:1080/files/", location
    )
    status, response_headers, raw = http(
        "PATCH",
        upload_url,
        body=wav_bytes,
        headers={
            "Tus-Resumable": "1.0.0",
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
            "X-Recantor-Upload-Token": token,
        },
        timeout=30,
    )
    assert status == 204, (status, raw[:500])
    assert int(response_headers.get("Upload-Offset", "-1")) == len(wav_bytes)

    auth = {"X-Recantor-Upload-Token": token}
    for _ in range(120):
        result = json_http(
            "GET", f"{API}/uploads/{session_id}/result", headers=auth
        )
        state = result.get("state")
        if state == "complete":
            assert result.get("exports_available") is True
            break
        if state in {"failed", "no_speech"}:
            raise AssertionError((session_id, result))
        time.sleep(1)
    else:
        raise AssertionError("upload processing did not reach complete")

    page = json_http(
        "GET", f"{API}/uploads/{session_id}/transcript?limit=100", headers=auth
    )
    assert page.get("segments"), page
    for export_format in ("txt", "json", "vtt", "srt"):
        status, _, body = http(
            "GET",
            f"{API}/uploads/{session_id}/exports/{export_format}",
            headers=auth,
        )
        assert status == 200 and body.strip(), (
            export_format,
            status,
            body[:100],
        )
    return session_id, token


def verify_archive(session_id: str, storage_key: str) -> None:
    state = json_http("GET", f"{API}/sessions/{session_id}/recording-state")
    assert state["accepted_count"] == 1, state
    assert state["highest_contiguous_sequence"] == 1, state
    program = (
        "from pathlib import Path; import os; "
        "p=Path('/data/audio')/os.environ['KEY']; "
        "assert p.is_file(), p; print('ok')"
    )
    result = compose(
        "exec",
        "-T",
        "-e",
        f"KEY={storage_key}",
        "api",
        "python",
        "-c",
        program,
        capture=True,
    )
    assert result.stdout.strip().endswith("ok")


def verify_upload_persistence(session_id: str, token: str) -> None:
    auth = {"X-Recantor-Upload-Token": token}
    result = json_http(
        "GET", f"{API}/uploads/{session_id}/result", headers=auth
    )
    assert result.get("state") == "complete", result
    page = json_http(
        "GET", f"{API}/uploads/{session_id}/transcript?limit=100", headers=auth
    )
    assert page.get("segments"), page
    for export_format in ("txt", "json", "vtt", "srt"):
        status, _, body = http(
            "GET",
            f"{API}/uploads/{session_id}/exports/{export_format}",
            headers=auth,
        )
        assert status == 200 and body.strip(), (export_format, status)

    program = r"""
import asyncio, os
from pathlib import Path
from uuid import UUID
from recantor.db import get_sessionmaker
from recantor.models import UploadMediaProcessing, UploadRecord, UploadProcessingState
from recantor.settings import get_settings

async def main():
    session_id = UUID(os.environ["SESSION_ID"])
    async with get_sessionmaker()() as db:
        record = await db.get(UploadRecord, session_id)
        processing = await db.get(UploadMediaProcessing, session_id)
    assert record and record.completed_at and record.storage_key
    assert processing and processing.state == UploadProcessingState.SUCCEEDED.value
    assert processing.normalized_storage_key
    root = Path(get_settings().audio_storage_path)
    assert (root / record.storage_key).is_file()
    assert (root / processing.normalized_storage_key).is_file()
    print("ok")
asyncio.run(main())
"""
    result = compose(
        "exec",
        "-T",
        "-e",
        f"SESSION_ID={session_id}",
        "api",
        "python",
        "-c",
        program,
        capture=True,
    )
    assert result.stdout.strip().endswith("ok")


def assert_zero_state() -> None:
    program = r"""
import asyncio
from pathlib import Path
from sqlalchemy import func, select
from recantor.db import get_sessionmaker
from recantor.models import (
    RecordingSession,
    TranscriptSegment,
    UploadMediaProcessing,
    UploadRecord,
)
from recantor.settings import get_settings

async def main():
    async with get_sessionmaker()() as db:
        counts = [
            int(await db.scalar(select(func.count(RecordingSession.id))) or 0),
            int(await db.scalar(select(func.count(UploadRecord.session_id))) or 0),
            int(await db.scalar(select(func.count(UploadMediaProcessing.session_id))) or 0),
            int(await db.scalar(select(func.count(TranscriptSegment.id))) or 0),
        ]
    assert counts == [0, 0, 0, 0], counts
    root = Path(get_settings().audio_storage_path)
    files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
    assert not files, files[:10]
    print("zero")
asyncio.run(main())
"""
    result = compose(
        "exec", "-T", "api", "python", "-c", program, capture=True
    )
    assert result.stdout.strip().endswith("zero")


def main() -> None:
    pass_count = 0
    with tempfile.TemporaryDirectory(prefix="recantor-alpha-cloud-") as temp:
        wav_bytes = make_wav(Path(temp) / "proof.wav")
        compose("down", "-v", "--remove-orphans", check=False)
        try:
            compose("config", "--quiet")
            compose("up", "--build", "-d")
            wait_stack()

            live_success = create_live()
            seed_utterance(live_success, "alpha:live:success", wav_bytes)
            wait_transcript(live_success)
            pass_count += 1
            print("A1_SECRETLESS_LIVE_SUCCESS_PASS")

            compose("stop", "stt-worker", "stt-reconciler", "redis")
            live_recovery = create_live()
            seed_utterance(live_recovery, "alpha:live:recovery", wav_bytes)
            assert not transcript(live_recovery).get("segments")
            compose("start", "redis")
            compose("start", "stt-worker", "stt-reconciler")
            wait_transcript(live_recovery)
            pass_count += 1
            print("A2_REDIS_WORKER_RECONCILER_RECOVERY_PASS")

            result = compose(
                "exec",
                "-T",
                "media-worker",
                "python",
                "/proof/alpha_media_runtime_proof.py",
                capture=True,
            )
            marker = "MEDIA_RUNTIME_PROOF_PASS supported=6 normalized=6 negative=2"
            assert marker in result.stdout, result.stdout
            pass_count += 8
            print(marker)

            archive_session, archive_key = create_archive_evidence(wav_bytes)
            pass_count += 1
            print("C1_ARCHIVE_DURABLE_EVIDENCE_PASS")

            upload_session, upload_token = create_upload_and_wait(wav_bytes)
            pass_count += 1
            print("C2_UPLOAD_MEDIA_TRANSCRIPT_RESULT_PASS")

            compose("down", "--remove-orphans")
            compose("up", "-d")
            wait_stack()
            wait_transcript(live_success)
            wait_transcript(live_recovery)
            verify_archive(archive_session, archive_key)
            verify_upload_persistence(upload_session, upload_token)
            pass_count += 1
            print("C3_WHOLE_STACK_RESTART_PERSISTENCE_PASS")

            compose("down", "-v", "--remove-orphans")
            compose("up", "-d")
            wait_stack()
            assert_zero_state()
            pass_count += 1
            print("C4_TRUE_ZERO_STATE_FRESH_BOOT_PASS")

            assert pass_count == 14, pass_count
            print("ALPHA_CLOUD_PROOF_COUNTS pass=14 fail=0 skip=0")
        except Exception:
            compose("ps", check=False)
            compose("logs", "--no-color", "--tail", "200", check=False)
            raise
        finally:
            compose("down", "-v", "--remove-orphans", check=False)


if __name__ == "__main__":
    main()
