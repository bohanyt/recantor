# Recantor

Recantor is a self-hosted recording and transcription project built around one rule: **capture safety is independent from downstream intelligence**.

The current cloud-alpha integration line has two web workflows:

- **Live** — reliable browser archive recording plus an independent realtime speech lane, durable STT scheduling, canonical transcript recovery, and live transcript display.
- **Upload recording** — resumable transfer of an existing WAV/MP3/M4A/OGG/WebM/MP4 file into durable upload storage. Uploaded-media normalization/transcription processing is not implemented yet.

## Reliability principles

1. Archive audio is persisted locally before upload and removed from the browser spool only after durable server ACK.
2. One capture writer owns a live session at a time; stale/fenced clients retain evidence but cannot mutate that session.
3. Finalization has an explicit high-water boundary; missing audio is never silently invented away.
4. Realtime STT and transcript delivery may degrade without weakening archive recording safety.
5. PostgreSQL and audio storage are durable truth. Redis/Celery and WebSocket delivery are coordination paths, not canonical meeting state.

Desktop Chrome/Edge on an awake computer is the first browser reliability target. Continuous capture through sleep, shutdown, execution-suspending lock behavior, or mobile background suspension is not claimed.

## Current stack

- Web: React + TypeScript + Vite + Tailwind CSS
- Browser recovery: Dexie / IndexedDB
- API: Python + FastAPI + Pydantic
- Database: PostgreSQL + SQLAlchemy 2 + Alembic
- Live STT scheduling: PostgreSQL-authoritative jobs with Celery + Redis delivery/reconciliation
- STT provider: server-side Groq Whisper adapter (`whisper-large-v3-turbo` by default)
- Transcript delivery: canonical HTTP cursor reads plus ephemeral WebSocket wake hints
- Existing-recording upload: Uppy + tus/tusd with durable Recantor completion evidence
- Development orchestration: Docker Compose
- Tooling: Node.js 24, pnpm 11.7, Python 3.13, uv 0.10

## Quick start

The development stack requires Docker with Docker Compose.

```bash
git clone https://github.com/bohanyt/recantor.git
cd recantor
cp .env.example .env
```

Open `.env` and set the server-side key:

```text
GROQ_API_KEY=your-groq-key-here
```

A fake value such as `fake-local-config-check` is sufficient to verify that Compose passes the variable into the API and STT worker, but real live transcription requires a valid Groq key. Recantor does not need a browser-side provider key and does not implement provider/fallback selectors in normal setup.

Then start the stack, passing the repository environment file explicitly:

```bash
docker compose --env-file .env -f infra/compose.yaml up --build
```

The explicit `--env-file .env` keeps setup truthful even though the Compose file lives under `infra/`. The current Compose file forwards `GROQ_API_KEY` to both `api` and `stt-worker`, while keeping it out of the web service.

Open:

- Web app: `http://localhost:5173`
- API docs: `http://localhost:8000/docs`
- Process liveness: `http://localhost:8000/healthz`
- PostgreSQL readiness: `http://localhost:8000/readyz`
- tus upload endpoint (protocol endpoint, not a human UI): `http://localhost:1080/files/`

Stop the stack:

```bash
docker compose --env-file .env -f infra/compose.yaml down
```

Delete development database/audio volumes too only when you intentionally want a clean slate:

```bash
docker compose --env-file .env -f infra/compose.yaml down -v
```

## What works on the current integration line

### Live recording and canonical transcript

The archive recorder uses `MediaRecorder`, persists emitted fragments to IndexedDB, uploads sequence-numbered audio with ownership/hash/timing evidence, keeps local evidence until durable server ACK, supports recovery/finalization, fences stale capture writers, and makes unresolved continuity explicit.

The independent realtime lane uses the same microphone stream through Web Audio/AudioWorklet, commits independently decodable durable utterance WAV work, and feeds the durable live STT scheduler. PostgreSQL-backed scheduling/claim/retry/fairness from Issue #38 is present on the integration line. Canonical `TranscriptSegment` rows remain the transcript source of truth; the #42 UI uses HTTP cursor recovery and WebSocket delivery only as a wake hint.

Archive-audio safety and transcription state are intentionally separate. A delayed transcript never means archive audio is unsafe, and a safe archive does not imply transcription succeeded.

### Existing-recording upload

The #44 upload foundation is present on the integration line. Uppy/tus can pause, reload, resume the same durable upload, and verifies Recantor server completion before showing the transfer as durably uploaded.

That is the current boundary. The following are **not** current upload capabilities:

- FFmpeg/ffprobe validation or normalization of uploaded media;
- segmentation and upload-to-STT processing (#45);
- uploaded-recording transcript/result/export UX (#46).

## Configuration truth

`apps/api/src/recantor/settings.py` is the backend settings authority. Current STT configuration is direct Groq configuration: `GROQ_API_KEY` plus optional endpoint/model/timeout overrides. There is no implemented `STT_PRIMARY_PROVIDER`, `STT_FALLBACK_PROVIDER`, or `LOCAL_STT_BASE_URL` selector in current backend settings.

`.env.example` documents implemented development inputs only. Keep real secrets in local environment configuration; never commit them.

## Development checks

Dependency resolutions are pinned in `apps/api/uv.lock` and `apps/web/pnpm-lock.yaml`.

Typical checks:

```bash
uv sync --project apps/api --frozen --dev
uv run --project apps/api ruff check apps/api
uv run --project apps/api ruff format --check apps/api
uv run --project apps/api pytest

pnpm --dir apps/web install --frozen-lockfile
# Start the API first so OpenAPI can be generated.
pnpm --dir apps/web generate:api
pnpm --dir apps/web lint
pnpm --dir apps/web format:check
pnpm --dir apps/web typecheck
pnpm --dir apps/web test
pnpm --dir apps/web build
```

GitHub Actions also exercises PostgreSQL migrations/readiness, Redis, Chromium web/API/PostgreSQL behavior, recorder recovery/fencing paths, durable live STT scheduling, resumable upload, and Docker Compose smoke coverage.

## Production exposure boundary

The current alpha is trusted-development software. Production authentication/authorization, retention/deletion behavior, abuse controls, and deployment hardening remain required before exposing live recordings or sensitive transcript data to untrusted users.

## Documentation

- [`docs/CURRENT.md`](docs/CURRENT.md) — operational source of truth and immediate next work
- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product scope and user-facing behavior
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — architecture and reliability contracts
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phased delivery plan
- [`docs/decisions/0001-foundation-stack.md`](docs/decisions/0001-foundation-stack.md) — foundation stack ADR
- [`docs/decisions/0002-recording-access-guardrails.md`](docs/decisions/0002-recording-access-guardrails.md) — recording/access guardrails
- [`AGENTS.md`](AGENTS.md) — working contract for humans and coding agents

## License

Apache-2.0. See [`LICENSE`](LICENSE).
