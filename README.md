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

The current Phase 2 core includes the realtime utterance/VAD path, canonical transcript storage, and a Groq Whisper STT provider boundary. Planned downstream capabilities include Celery/Redis live STT processing, realtime transcript delivery, faster-whisper local fallback, diarization, meeting summaries, FFmpeg normalization, and tus-based resumable existing-file uploads. They are not current capability claims unless `docs/CURRENT.md` says otherwise.

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

The reliable archive recorder is implemented. It uses `MediaRecorder`, persists emitted fragments to a Dexie/IndexedDB recovery spool, uploads sequenced audio to the API, keeps local evidence until durable server ACK, supports recovery/finalization, fences stale capture writers, and makes unresolved continuity/gaps explicit.

A clean Stop declares a final high-water boundary; the server reaches `COMPLETE` only after expected sequences are durably present or explicitly represented as loss. Persisted `audio_completeness` distinguishes full, partial, and empty terminal audio evidence.

The browser also has an independent realtime lane that taps the same microphone stream through Web Audio/AudioWorklet, sends sample-clock PCM to the server, endpoints speech with a bounded VAD baseline, and stores independently decodable durable utterance WAV work. The merged STT boundary can verify one of those durable utterances, send it to Groq Whisper, and commit one retry-safe canonical transcript segment. Realtime/STT failure remains downstream degradation and does not participate in archive ACK semantics.

## Current project status

**Phase 1 capture reliability is closed.** The bounded real-platform witness used an ordinary Microsoft Edge window on an awake Windows 11 laptop with a real microphone and proved archive capture/ACK progress through a whole-window minimize interval. This does not claim continuous capture through desktop sleep/shutdown, execution-suspending lock behavior, or mobile browser suspension.

**Phase 2 foundations through real provider execution are merged.** The repository now has:

- terminal audio completeness;
- canonical PostgreSQL transcript segments with reconnect cursor semantics;
- durable independently decodable `TranscriptionUtterance` work;
- a real browser PCM/VAD utterance producer, validated on Windows/Edge with 71 durable WAV utterances while archive capture remained `COMPLETE`/`full`;
- an owned provider-neutral STT boundary with Groq `whisper-large-v3-turbo` as the first adapter;
- a bounded real-provider witness where durable real-microphone utterances produced canonical transcript rows without weakening archive capture.

PR #37 merged as `f0a0f2e068cb1003619010595928c0273912e61c`; post-merge CI run `34456347492` succeeded across backend, frontend, Chromium E2E, and Compose smoke.

**Issue #38 is the current Phase 2E slice:** add automatic durable live STT queueing, PostgreSQL-backed claim/reconciliation semantics, provider retry handling, and multi-session fairness using Celery/Redis only as delivery/coordination rather than durable truth. Realtime transcript fanout/UI, local faster-whisper fallback, diarization, summaries, production auth, and native mobile recording remain downstream.

See [`docs/CURRENT.md`](docs/CURRENT.md) for exact evidence and immediate next work.

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
