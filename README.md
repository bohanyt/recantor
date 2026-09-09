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

## Planned stack

- Web: React + TypeScript + Vite + Tailwind CSS
- Server state: TanStack Query
- Browser recovery spool: IndexedDB via Dexie
- API: Python + FastAPI + Pydantic
- Database: PostgreSQL + SQLAlchemy 2 + Alembic
- Jobs: Celery + Redis
- Realtime delivery: WebSocket where appropriate
- Resumable guest uploads: Uppy + tus/tusd
- Primary STT: Groq Whisper API
- Local STT fallback: faster-whisper / CTranslate2
- Audio normalization: FFmpeg
- Deployment baseline: Docker Compose + Caddy

The public upstream stays deployment-agnostic. Organization-specific branding, domains, authentication policy, infrastructure, and secrets belong in deployment configuration or downstream forks/overlays.

## Reliability boundary

Browser live recording uses a local recovery spool plus sequenced server ingestion. A server ACK is the durable boundary; browser IndexedDB may still be subject to browser persistence/quota behavior.

A clean Stop declares a final sequence/high-water mark so completion can be proven rather than inferred from silence.

Desktop Chrome/Edge on an awake computer is the first web reliability target. Mobile web remains useful while active, while future Android/iOS recorder clients provide the stronger background/lock-screen capture path.

## Project status

**Foundation / pre-implementation.** Product and architecture contracts are established and audited; the next bounded step is the runnable application scaffold in Issue #2.

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
