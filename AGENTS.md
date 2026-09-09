# AGENTS.md

This file is the front door for humans and coding agents working on Recantor.

## Read order

Before changing code or architecture, read:

1. `docs/CURRENT.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. relevant ADRs under `docs/decisions/`
5. the issue or task being implemented

GitHub is the technical source of truth. Do not rely on stale chat history, screenshots, local notes, or an old handoff when repository state disagrees.

## Product invariants

These are not optional implementation details.

### 1. Capture is infrastructure; intelligence is downstream

Recording must not depend on STT, diarization, summaries, or LLM availability.

If Groq is unavailable, audio must still be safe.
If local GPUs are unavailable, audio must still be safe.
If the summary worker is delayed, recording and transcription must continue.

### 2. The server owns sessions

A browser tab is not the source of truth for a recording session. A session has a server-side identity and lifecycle. Browser, Android, iOS, and future desktop clients must use the same protocol semantics.

### 3. Durable audio precedes acknowledgement

For browser live recording, chunks are locally buffered and uploaded with monotonically increasing sequence numbers. A client may discard a local chunk only after receiving a durable server acknowledgement.

Browser IndexedDB is a recovery spool, not the final durability boundary. If local persistence/quota fails while the server is unreachable, the UI must stop claiming the recording is safe.

Server ingestion must be idempotent. Retrying the same `(session_id, sequence)` must not duplicate audio or transcript state.

### 4. One active capture writer per live session

Concurrent sessions are normal, but two tabs/devices must not silently generate competing live sequence streams for the same session.

Use same-origin coordination in the browser where available and a server-authoritative capture lease/epoch or equivalent ownership mechanism across clients. Recovery upload of already-spooled chunks is separate from ownership of new capture.

### 5. Finalization has a completeness boundary

A clean Stop/finalize declares a final sequence/high-water mark. A session may become complete only after every expected sequence through that boundary is either durably present or explicitly represented as a gap/failure.

Browser disappearance without a clean Stop creates an interruption, not an inferred successful completion.

### 6. Gaps are explicit

If Recantor cannot prove audio continuity, it must surface a gap. Never fabricate continuity or silently hide an interrupted interval.

### 7. Concurrent users are normal

Do not build global mutable recorder state. Every recording, transcript, summary, and job belongs to a session. Shared workers must be bounded and fair enough that one long meeting does not monopolize the system.

### 8. Durable state is not Redis

PostgreSQL and audio storage hold durable state. Redis is for queues, short-lived coordination, locks, rate limiting, and similar ephemeral concerns.

## Initial stack contract

Until superseded by an ADR:

- frontend: React + TypeScript + Vite + Tailwind CSS
- server state: TanStack Query
- backend: Python + FastAPI + Pydantic
- persistence: PostgreSQL + SQLAlchemy 2 + Alembic
- jobs: Celery + Redis
- browser recovery spool: Dexie / IndexedDB
- primary STT: Groq Whisper API
- local STT fallback: faster-whisper / CTranslate2
- audio processing: FFmpeg
- resumable large uploads: Uppy + tus/tusd
- deployment baseline: Docker Compose + Caddy
- initial runtimes: Node.js 24 LTS and Python 3.13 for the main API

GPU/ML services may use a different supported Python minor if selected dependencies require it; keep that exception inside the service/runtime boundary and document it.

Do not introduce a second backend framework, a second durable database, Kubernetes, Kafka, GraphQL, or additional distributed infrastructure without an ADR showing a concrete requirement that the current design cannot meet.

## Contract-first development

Prefer explicit types and generated contracts over duplicated handwritten shapes.

- Product HTTP APIs live under `/api/v1` from the beginning.
- `/healthz` is process/liveness only.
- `/readyz` is dependency readiness.
- FastAPI/Pydantic owns HTTP API schemas.
- OpenAPI is generated from the API.
- Web API types/clients are generated from OpenAPI once application scaffolding exists.
- Database schema changes require Alembic migrations.
- External providers live behind Recantor-owned interfaces.

Examples of provider boundaries:

- `STTProvider`
- `AudioStorage`
- `SummaryProvider`
- `DiarizationProvider`

Product/domain code must not depend directly on one vendor when a provider boundary is appropriate.

## Recording critical path

Treat the following as the reliability-critical path:

`capture -> local recovery spool -> upload -> durable server write + durable acceptance metadata -> ACK`

STT, diarization, live UI updates, and summaries are downstream consumers.

A change that improves live latency but weakens the durable path is not acceptable.

Do not assume `MediaRecorder` chunks are independently decodable standalone media files. Preserve ordering/container semantics and use a validated realtime/decoder path for STT.

## Job semantics

Background jobs must be retryable and idempotent where possible.

Do not assume Celery delivers exactly once. Database uniqueness constraints and state transitions should prevent duplicated output.

Keep latency classes separate conceptually:

- live STT: high priority
- finalization / repair: normal priority
- rolling summary: lower priority

A slow LLM queue must never block audio ingestion.

## Browser behavior

Desktop Chrome/Edge is the primary web recording target for the first production path. Responsive mobile web remains supported for active-page use.

Do not claim that a mobile browser can guarantee background or lock-screen recording. Future native Android/iOS recorder clients exist specifically for persistent mobile capture.

Web UX should use capability-aware warnings rather than pretending unsupported lifecycle behavior is reliable.

For the browser recovery spool, request/check persistent storage when supported and monitor approximate quota/usage. Storage failure must become visible recording state.

## Access and privacy

The Live Intelligence product is authenticated before production exposure. A trusted development proof may temporarily run without auth, but that is not a production capability claim.

Meeting audio/transcripts are sensitive data. Before production exposure, owned recordings need authorization, explicit retention behavior, and a user-visible deletion path.

Guest access uses bounded high-entropy capability tokens rather than public/guessable URLs.

Recantor is not designed for covert recording. Active recording state must be obvious to the operator. Deployment operators remain responsible for applicable notice/consent requirements.

## Public upstream boundary

This repository is public and reusable.

Do not commit:

- organization secrets
- API keys
- private domains that are not intended as public documentation
- internal user data
- employee voiceprints
- production credentials
- downstream-only branding assets

Organization-specific branding, auth policy, deployment names, and infrastructure should remain configurable or live in a downstream deployment repository/fork.

## Third-party code

Research and architectural inspiration are welcome, but do not copy code from another project unless its license and attribution requirements are understood and satisfied.

Record meaningful third-party adoption in `docs/REFERENCES.md` and/or an ADR.

## Change discipline

For non-trivial work:

1. establish the current repository truth;
2. identify the relevant product/architecture contract;
3. make the smallest coherent change;
4. add or update tests for behavior being introduced;
5. update `docs/CURRENT.md` when current truth changes materially;
6. update architecture/product docs when contracts change;
7. prefer a focused branch and PR over unrelated changes bundled together.

## Definition of done

A feature is not done because its happy-path code exists. Depending on scope, completion may require:

- tests green;
- migrations included;
- retry/idempotency behavior proven;
- reconnect/interruption behavior tested;
- multi-session isolation tested;
- browser behavior exercised in a real browser;
- documentation updated;
- failure states visible rather than silently swallowed.

When evidence is incomplete, say so explicitly.
