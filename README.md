# Recantor

Recantor is a self-hosted recording and meeting-intelligence platform focused on reliable audio capture first, then transcription, diarization, summaries, and exports.

The project is designed for two primary workflows:

- **Live Intelligence** — authenticated browser recording, live transcript updates, speaker processing, and rolling meeting intelligence.
- **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then produce a durable transcript and exports. A bounded guest path may operate without login.

## Core principles

1. **Capture is infrastructure. Intelligence is downstream.** Recording must remain safe even if STT, diarization, an LLM, or the network is degraded.
2. **The server owns the session.** Browsers and future native apps are clients of the same recording protocol.
3. **Durable before clever.** Audio is persisted and acknowledged before downstream processing is considered successful.
4. **Concurrent users are normal.** Work is isolated by session and processed through bounded queues and worker pools.
5. **Failures must be visible.** Missing audio is reported as a gap; Recantor must never silently pretend a recording is complete.
6. **Boring, documented technology wins.** Prefer mainstream tools with clear contracts, tests, and operational behavior.

## Stack

Current application foundation:

- Web: React + TypeScript + Vite + Tailwind CSS
- Server state: TanStack Query
- API: Python + FastAPI + Pydantic
- Database: PostgreSQL + SQLAlchemy 2 + Alembic
- Development orchestration: Docker Compose
- Tooling: Node.js 24, pnpm 11.7, Python 3.13, uv 0.10
- API contract: FastAPI OpenAPI -> generated TypeScript client

Planned downstream capabilities:

- Browser recovery spool: IndexedDB via Dexie
- Jobs: Celery + Redis
- Realtime delivery: WebSocket where appropriate
- Resumable guest uploads: Uppy + tus/tusd
- Primary STT: Groq Whisper API
- Local STT fallback: faster-whisper / CTranslate2
- Audio normalization: FFmpeg
- Default production reverse proxy: Caddy, replaceable downstream

The public upstream stays deployment-agnostic. Organization-specific branding, domains, authentication policy, infrastructure, and secrets belong in deployment configuration or downstream forks/overlays.

## Quick start

The Phase 0 development stack requires Docker with Docker Compose.

```bash
git clone https://github.com/bohanyt/recantor.git
cd recantor
docker compose -f infra/compose.yaml up --build
```

Compose starts PostgreSQL and Redis, runs Alembic migrations, then starts the API and web application.

Open:

- Web: `http://localhost:5173`
- API docs: `http://localhost:8000/docs`
- Process liveness: `http://localhost:8000/healthz`
- PostgreSQL readiness: `http://localhost:8000/readyz`

Stop the stack:

```bash
docker compose -f infra/compose.yaml down
```

Delete the development database volume too:

```bash
docker compose -f infra/compose.yaml down -v
```

The current UI is intentionally only a foundation/status shell. It does **not** record audio yet.

## Development checks

The repository pins dependency resolutions in `apps/api/uv.lock` and `apps/web/pnpm-lock.yaml`.

Typical checks are:

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

GitHub Actions additionally proves PostgreSQL migrations/readiness, Redis reachability, a real Chromium web -> API -> PostgreSQL smoke path, and the Docker Compose development path.

## Reliability boundary

Browser live recording will use a local recovery spool plus sequenced server ingestion. A server ACK is the durable boundary; browser IndexedDB may still be subject to browser persistence/quota behavior.

A clean Stop will declare a final sequence/high-water mark so completion can be proven rather than inferred from silence.

Desktop Chrome/Edge on an awake computer is the first web reliability target. Mobile web remains useful while active, while future Android/iOS recorder clients provide the stronger background/lock-screen capture path.

## Project status

**Phase 0 application foundation.** The runnable web/API/database/Redis/Compose/CI baseline is implemented. Recording, STT, diarization, summaries, uploads, and production authentication remain intentionally unimplemented.

Start here:

- [`docs/CURRENT.md`](docs/CURRENT.md) — current project truth and immediate next step
- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product scope and user-facing behavior
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture and reliability contracts
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phased delivery plan
- [`docs/decisions/0001-foundation-stack.md`](docs/decisions/0001-foundation-stack.md) — initial stack decision
- [`docs/decisions/0002-recording-access-guardrails.md`](docs/decisions/0002-recording-access-guardrails.md) — browser recording/access guardrails
- [`AGENTS.md`](AGENTS.md) — working contract for humans and coding agents

## License

Apache-2.0. See [`LICENSE`](LICENSE).
