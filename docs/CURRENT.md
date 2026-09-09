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

The durable recording path is being delivered as:

```text
capture -> browser recovery spool -> sequenced upload
        -> durable server audio + acceptance metadata -> ACK
```

The server-side half of that contract is now implemented. The browser capture/recovery half is the next Phase 1 slice.

STT, diarization, live transcript delivery, and LLM summaries are downstream and may degrade without invalidating already acknowledged audio.

Non-optional recording guardrails:

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

### Phase 0 — complete

The runnable application foundation is implemented and CI-proven:

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

### Phase 1A — server durability core implemented and CI-proven

The server-side recording contract now includes:

- durable PostgreSQL live-session lifecycle;
- explicit active writer ID and capture epoch;
- competing/stale writer rejection for new capture;
- heartbeat-based interruption derivation without inferring successful completion;
- sequenced `/api/v1` binary chunk ingestion;
- per-chunk writer/epoch/timing/content/hash identity evidence;
- strict retry idempotency: a sequence can be retried only with matching accepted metadata and bytes;
- filesystem `AudioStorage` using temp write, file flush/fsync, atomic replace, parent-directory fsync where available, and post-write integrity verification;
- PostgreSQL acceptance metadata committed before the HTTP ACK is returned;
- safe retry through the file-before-database crash window by reusing an identical orphaned durable file;
- persistent Compose audio volume;
- server reconciliation of accepted sequence ranges and highest contiguous sequence;
- explicit sequence/wall-clock gap declarations with immutable idempotency keys;
- final sequence/high-water-mark and monotonic-time boundaries;
- rejection of a final boundary that excludes already accepted audio;
- finalization that remains `FINALIZING` while expected sequences are missing;
- explicit unrecoverable gaps as an alternative to fabricating continuity;
- retry-safe completed finalization tied to the original finalizing writer/epoch;
- two independent recording sessions proven isolated in automated tests.

GitHub Actions run `34308690997` is the Phase 1A witness: backend, frontend, Chromium e2e, and Compose smoke all completed successfully after the final architecture/idempotency review changes.

This evidence proves the **server-side recording contract**, not browser microphone capture or end-user recording reliability yet.

### Not implemented/proven yet

- browser microphone recording;
- Dexie/IndexedDB recording recovery spool;
- browser persistent-storage/quota handling;
- browser retry/backoff and ACK-driven spool deletion;
- refresh/reconnect browser recovery;
- same-origin Web Locks/fallback tab coordination;
- recorder UI durability/degraded states;
- browser Stop flushing its final MediaRecorder event before finalization;
- deterministic browser recovery/network-loss tests;
- bounded real desktop Chrome/Edge background/minimized recording witness;
- cross-device capture takeover after an interrupted writer;
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

GitHub Issue #5 remains open for **Phase 1: reliable desktop browser recording and recovery**.

The immediate next slice is **Phase 1B: browser recorder and recovery client**, built on the Phase 1A server contract. It must add and prove:

- microphone permission/capability handling and MediaRecorder capture;
- Dexie/IndexedDB local recovery spool before upload;
- stable session/writer identity across refresh recovery;
- persistent-storage request/status and quota monitoring;
- monotonically sequenced chunks with Web Crypto SHA-256;
- bounded retry/backoff;
- delete-local-only-after-durable-ACK behavior;
- server/client accepted-sequence reconciliation after reconnect;
- same-origin tab coordination plus server-authoritative writer rejection;
- heartbeat/interruption recovery behavior;
- clean Stop that waits for the final `dataavailable`, persists it, flushes pending uploads, and then finalizes the declared high-water mark;
- visible pending/synced/degraded/unsafe/gap states;
- deterministic Playwright recovery scenarios where browser automation can prove them.

The final Phase 1 exit still requires a bounded real Chrome/Edge background/minimized witness on an awake desktop. CI must not be used to overclaim that operating-system/browser-lifecycle behavior.

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
- Current Phase 1A recovery ownership is same-writer/same-epoch; cross-device takeover is intentionally not claimed.
- Public upstream must remain free of deployment secrets and organization-private data.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
