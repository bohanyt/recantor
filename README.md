# Recantor

Recantor is a self-hosted recording and meeting-intelligence platform focused on reliable audio capture first, then transcription, diarization, summaries, and exports.

The project is designed for two primary workflows:

- **Live Intelligence** — authenticated browser recording, live transcript updates, speaker processing, and rolling meeting intelligence.
- **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then produce a durable transcript and exports. A bounded guest path may operate without login.

## Core principles

1. **Capture is infrastructure. Intelligence is downstream.** Recording must remain safe even if STT, diarization, an LLM, or the network is degraded.
2. **The server owns the session.** Browsers and future native apps are clients of the same recording protocol.
3. **Durable before clever.** Audio is persisted and acknowledged before downstream processing is considered successful.
4. **Concurrent users are normal.** Work is isolated by session.
5. **Failures must be visible.** Missing audio is reported honestly; Recantor must never silently pretend continuity.

## Current stack

- Web: React + TypeScript + Vite + Tailwind CSS
- Server state: TanStack Query
- Browser recovery spool: Dexie / IndexedDB
- API: Python + FastAPI + Pydantic
- Database: PostgreSQL + SQLAlchemy 2 + Alembic
- Development orchestration: Docker Compose
- Tooling: Node.js 24, pnpm 11.7, Python 3.13, uv 0.10
- API contract: FastAPI OpenAPI -> generated TypeScript client

Planned downstream capabilities include Celery/Redis processing, Groq Whisper STT, faster-whisper local fallback, realtime transcript delivery, diarization, meeting summaries, FFmpeg normalization, and tus-based resumable existing-file uploads. They are not current capability claims.

## Quick start

The development stack requires Docker with Docker Compose.

```bash
git clone https://github.com/bohanyt/recantor.git
cd recantor
docker compose -f infra/compose.yaml up --build
```

Compose starts PostgreSQL and Redis, runs Alembic migrations, then starts the API and web application.

Open:

- Web recorder: `http://localhost:5173`
- API docs: `http://localhost:8000/docs`
- Process liveness: `http://localhost:8000/healthz`
- PostgreSQL readiness: `http://localhost:8000/readyz`

Stop the stack:

```bash
docker compose -f infra/compose.yaml down
```

Delete development database/audio volumes too when you intentionally want a clean slate:

```bash
docker compose -f infra/compose.yaml down -v
```

## What the recorder currently does

The Phase 1 web recorder is implemented. It uses `MediaRecorder`, persists emitted fragments to a Dexie/IndexedDB recovery spool, uploads sequenced audio to the API, keeps local evidence until durable server ACK, supports recovery/finalization, fences stale capture writers, and makes unresolved continuity/gaps explicit.

A clean Stop declares a final high-water boundary; the server reaches `COMPLETE` only after expected sequences are durably present or explicitly represented as loss.

Heartbeat/liveness interruption is tracked separately from proven audio discontinuity. Losing heartbeat does not automatically fabricate an audio gap.

## Current project status

**Phase 1 capture reliability is closed.** Issue #5 passed its final bounded real-platform witness on 2026-09-10 using an ordinary Microsoft Edge window on an awake Windows 11 laptop with a real microphone. The whole browser window was minimized for about five minutes; capture/ACK progress continued, finalization reached `COMPLETE`, the browser had zero pending local fragments and zero explicit gaps, and the PostgreSQL ledger was contiguous through the final sequence.

That witness is deliberately narrow: it proves the tested awake desktop Edge behavior only. It does not claim continuous capture through desktop sleep/shutdown, screen lock that suspends execution, or mobile browser background suspension.

**Issue #15 is the next gate** and must establish persisted terminal completeness/continuity classification before the first downstream STT/summary consumer is implemented. Groq/Whisper STT, diarization, LLM summaries, production auth, and native mobile recording are not implemented yet.

See [`docs/CURRENT.md`](docs/CURRENT.md) for the exact witness evidence, operational state, and next step.

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

GitHub Actions additionally proves PostgreSQL migrations/readiness, Redis reachability, Chromium web -> API -> PostgreSQL smoke behavior, API restart durability, and the Docker Compose path.

## Reliability boundary

Desktop Chrome/Edge on an awake computer is the first web reliability target. Mobile web is useful while active, but Recantor does not claim continuous recording through mobile background suspension, desktop sleep, or shutdown.

The public upstream stays deployment-agnostic. Organization-specific branding, domains, authentication policy, infrastructure, and secrets belong in deployment configuration or downstream forks/overlays.

## Documentation

- [`docs/CURRENT.md`](docs/CURRENT.md) — operational source of truth and immediate next step
- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product scope and user-facing behavior
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — architecture and reliability contracts
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phased delivery plan
- [`docs/decisions/0001-foundation-stack.md`](docs/decisions/0001-foundation-stack.md) — initial stack decision
- [`docs/decisions/0002-recording-access-guardrails.md`](docs/decisions/0002-recording-access-guardrails.md) — recording/access guardrails
- [`AGENTS.md`](AGENTS.md) — working contract for humans and coding agents

## License

Apache-2.0. See [`LICENSE`](LICENSE).
