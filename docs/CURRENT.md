# Current

Last updated: 2026-09-09

This file is the short operational source of truth for the current Recantor state. Read it before planning implementation work, then verify the repository itself.

## Current product truth

Recantor is a public, self-hosted recording and meeting-intelligence project.

The agreed product shape has two primary web workflows:

1. **Live Intelligence** — authenticated live recording, transcript, speaker processing, and rolling meeting intelligence.
2. **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then process/export it. A bounded guest path may operate without login.

Desktop Chrome/Edge is the first reliability target for live web recording. Mobile web remains responsive and usable while active, but Recantor does not promise reliable mobile browser recording under screen lock/background suspension.

Future Android/iOS recorder clients will reuse the same server session/ingest protocol for persistent mobile capture.

## Current architecture truth

The foundation stack is:

- React + TypeScript + Vite + Tailwind CSS web SPA;
- TanStack Query for server state;
- Python + FastAPI + Pydantic API;
- PostgreSQL + SQLAlchemy 2 + Alembic durable structured state;
- Celery + Redis background processing;
- Dexie/IndexedDB local browser recovery spool;
- Uppy + tus/tusd resumable existing-recording upload;
- Groq Whisper API as primary STT;
- faster-whisper/CTranslate2 as local STT fallback;
- FFmpeg for media normalization;
- Docker Compose deployment baseline;
- Caddy as the default/simple reverse proxy, replaceable downstream;
- Node.js 24 LTS for initial web/tooling and Python 3.13 for the main API.

Architecture invariants are defined in `docs/ARCHITECTURE.md`, `docs/decisions/0002-recording-access-guardrails.md`, and `AGENTS.md`.

Most important:

> Capture is infrastructure. Intelligence is downstream.

The durable recording path is conceptually:

```text
capture -> local recovery spool -> sequenced upload -> durable server write -> ACK
```

STT, diarization, live transcript delivery, and LLM summaries are downstream and may degrade without invalidating already acknowledged audio.

The foundation audit added these non-optional guardrails before recording implementation:

- browser IndexedDB is a recovery spool and must expose persistence/quota failure rather than pretending it is equivalent to server durability;
- only one active capture writer may own a live session at once;
- Stop/finalize declares a final sequence/high-water mark;
- a server ACK requires durable audio plus durable acceptance metadata;
- raw MediaRecorder chunks must not be assumed independently decodable;
- product API routes start versioned under `/api/v1`;
- `/healthz` is process liveness and `/readyz` is dependency readiness;
- Live Intelligence must gain authentication/ownership before production exposure;
- recording deletion/retention and visible recording state are part of the production privacy baseline.

## Repository state

The repository is still in **Phase 0: Foundation**.

Present:

- Apache-2.0 license;
- project README;
- product contract;
- architecture contract;
- roadmap;
- agent working contract;
- stack ADR;
- recording/access guardrail ADR;
- third-party reference notes.

Not implemented yet:

- application scaffold;
- web UI;
- FastAPI server;
- PostgreSQL schema;
- recording protocol;
- background workers;
- STT;
- diarization;
- summaries;
- upload pipeline;
- authentication;
- production deployment.

Do not describe any of those as working until repository evidence proves it.

## Immediate next delivery

The next bounded work is GitHub Issue #2: **Phase 0 application scaffold**, not STT implementation.

It should create the smallest runnable end-to-end skeleton:

```text
apps/web       React + TypeScript + Vite
apps/api       FastAPI + Pydantic
infra          local Docker Compose baseline
```

With:

- a web page that can reach the API;
- `/healthz` process liveness and `/readyz` PostgreSQL readiness;
- product API namespace reserved under `/api/v1`;
- generated OpenAPI -> TypeScript contract used by the web app;
- PostgreSQL connectivity and migration tooling;
- Redis connectivity available for later workers;
- initial lint/typecheck/test commands;
- CI that runs those checks;
- pinned/documented runtime/tooling expectations;
- `.env.example` with no secrets;
- clear local development instructions.

Do **not** add microphone recording, Groq, GPU STT, diarization, LLM, or production authentication dependencies in the first scaffold unless required to prove the base contracts.

After the scaffold is proven, the next product slice is Phase 1 reliable web recording.

## Open decisions intentionally deferred

These should be resolved by evidence/ADR when their implementation phase begins:

- exact authentication implementation / OIDC or local-account strategy;
- exact LLM provider(s) for rolling summaries;
- final live diarization backend;
- final offline diarization backend;
- speaker embedding model and confidence calibration;
- production object-storage backend;
- exact queue concurrency/routing values;
- exact VAD/utterance timing after benchmark;
- exact mobile native framework;
- downstream organization-specific branding and infrastructure.

## Known design boundaries

- Browser background/minimized recording on an awake desktop is a core web use case.
- Closing the browser, sleeping/shutting down the computer, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect unsafe/degraded recovery state.
- WebSocket is for realtime updates, not durable state.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are a normal operating condition, not an edge case.
- A single live session has one active capture writer at a time.
- Public upstream must remain free of deployment secrets and organization-private data.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
