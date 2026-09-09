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

- React + TypeScript + Vite web SPA;
- Python + FastAPI + Pydantic API;
- PostgreSQL + SQLAlchemy 2 + Alembic durable structured state;
- Celery + Redis background processing;
- Dexie/IndexedDB local browser spool;
- Uppy + tus/tusd resumable existing-recording upload;
- Groq Whisper API as primary STT;
- faster-whisper/CTranslate2 as local STT fallback;
- FFmpeg for media normalization;
- Docker Compose deployment baseline;
- Caddy as the default/simple reverse proxy, replaceable downstream.

Architecture invariants are defined in `docs/ARCHITECTURE.md` and `AGENTS.md`.

Most important:

> Capture is infrastructure. Intelligence is downstream.

The durable recording path is conceptually:

```text
capture -> local spool -> sequenced upload -> durable server write -> ACK
```

STT, diarization, live transcript delivery, and LLM summaries are downstream and may degrade without invalidating already acknowledged audio.

## Repository state

At this checkpoint the repository is still in **Phase 0: Foundation**.

Present/being established:

- Apache-2.0 license;
- project README;
- product contract;
- architecture contract;
- roadmap;
- agent working contract;
- stack ADR;
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
- production deployment.

Do not describe any of those as working until repository evidence proves it.

## Immediate next delivery

The next bounded work is **Phase 0 application scaffold**, not STT implementation.

It should create the smallest runnable end-to-end skeleton:

```text
apps/web       React + TypeScript + Vite
apps/api       FastAPI + Pydantic
infra          local Docker Compose baseline
```

With:

- a web page that can reach the API;
- API `/health` (or equivalent) endpoint;
- PostgreSQL connectivity and migration tooling;
- Redis connectivity available for later workers;
- initial lint/typecheck/test commands;
- CI that runs those checks;
- `.env.example` with no secrets;
- clear local development instructions.

Do **not** add microphone recording, Groq, GPU STT, diarization, or LLM dependencies in the first scaffold unless required to prove the base contracts.

After the scaffold is proven, the next product slice is Phase 1 reliable web recording.

## Open decisions intentionally deferred

These should be resolved by evidence/ADR when their implementation phase begins:

- exact authentication implementation / OIDC provider;
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
- WebSocket is for realtime updates, not durable state.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are a normal operating condition, not an edge case.
- Public upstream must remain free of deployment secrets and organization-private data.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
