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

The selected foundation stack is:

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
- Caddy as the default/simple production reverse proxy, replaceable downstream;
- Node.js 24 LTS for initial web/tooling and Python 3.13 for the main API.

Not every selected component is implemented yet. The repository state below distinguishes proven runtime from planned downstream stack.

Architecture invariants are defined in `docs/ARCHITECTURE.md`, `docs/decisions/0002-recording-access-guardrails.md`, and `AGENTS.md`.

Most important:

> Capture is infrastructure. Intelligence is downstream.

The future durable recording path is:

```text
capture -> local recovery spool -> sequenced upload -> durable server write -> ACK
```

STT, diarization, live transcript delivery, and LLM summaries are downstream and may degrade without invalidating already acknowledged audio.

Non-optional recording guardrails already established before implementation:

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

**Phase 0 application foundation is implemented and CI-proven.**

Implemented:

- React + TypeScript + Vite + Tailwind web shell;
- TanStack Query server-state usage;
- FastAPI/Pydantic API;
- `/healthz` process liveness;
- `/readyz` PostgreSQL readiness, including a negative 503 proof when PostgreSQL is unavailable;
- versioned `/api/v1` product namespace;
- OpenAPI-generated TypeScript API client used by the web application;
- PostgreSQL connectivity through SQLAlchemy 2;
- Alembic migration history and migration execution;
- Redis development service and CI reachability proof;
- pinned Node/Python/tooling baselines and committed `uv`/`pnpm` lockfiles;
- Docker images using frozen dependency installs;
- Docker Compose development stack with PostgreSQL, Redis, one-shot migration, API, and web services;
- backend tests/lint/format checks;
- frontend lint/format/typecheck/unit/build checks;
- real Chromium Playwright smoke from the web shell through the API to PostgreSQL readiness;
- Docker Compose build/start/readiness/web-shell smoke in CI.

The Phase 0 CI witness is intentionally bounded: it proves the scaffold and development path, not recording behavior.

Not implemented yet:

- microphone recording;
- Dexie/IndexedDB recording spool;
- live session/chunk protocol;
- capture ownership lease/epoch;
- durable audio ACK path;
- background/Celery workers;
- Groq or local STT;
- diarization;
- summaries;
- tus upload pipeline;
- authentication/authorization;
- production deployment/reverse proxy;
- native mobile clients.

Do not describe any of those as working until repository evidence proves it.

## Immediate next delivery

GitHub Issue #5: **Phase 1: reliable desktop browser recording and recovery**.

Phase 1 is the first durability-critical product slice. It must implement and prove:

- durable live session lifecycle in PostgreSQL;
- one active capture writer per live session;
- browser microphone capture;
- Dexie/IndexedDB local recovery spool;
- persistent-storage/quota safety state;
- monotonically sequenced, idempotent chunk ingestion;
- filesystem `AudioStorage` with durable ACK semantics;
- retry/reconnect and server/client acknowledgement reconciliation;
- heartbeat/interruption state;
- final sequence/high-water-mark finalization;
- explicit gaps when continuity cannot be proven;
- two simultaneous independent sessions;
- same-session competing-tab rejection;
- bounded real-browser desktop background/minimized witness.

Do **not** pull Groq, Whisper, diarization, or LLM summaries into Phase 1. The recording path must be trustworthy independently first.

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

- Browser background/minimized recording on an awake desktop is a core web use case to prove in Phase 1.
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
