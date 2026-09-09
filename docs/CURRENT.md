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

The implemented browser/server durability path is now:

```text
MediaRecorder
  -> atomic Dexie/IndexedDB chunk + local high-water commit
  -> sequenced HTTP upload with hash/timing/ownership evidence
  -> crash-safe filesystem audio commit + PostgreSQL acceptance metadata
  -> HTTP ACK
  -> local chunk deletion
```

STT, diarization, live transcript delivery, and LLM summaries remain downstream and are intentionally absent from Phase 1.

Non-optional recording guardrails:

- browser IndexedDB is a recovery spool and must expose persistence/quota failure rather than pretending it is equivalent to server durability;
- only one active capture writer may own a live session at once;
- Stop/finalize declares a final sequence/high-water mark;
- a server ACK requires durable audio plus durable acceptance metadata;
- raw MediaRecorder chunks are ordered media fragments and must not be assumed independently decodable;
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
- OpenAPI-generated TypeScript API client contract;
- PostgreSQL connectivity through SQLAlchemy 2;
- Alembic migration history and execution;
- Redis development service and CI reachability proof;
- pinned Node/Python/tooling baselines and committed `uv`/`pnpm` lockfiles;
- Docker images using frozen dependency installs;
- Docker Compose development stack with PostgreSQL, Redis, one-shot migration, API, and web services;
- backend and frontend lint/format/typecheck/test/build gates;
- real Chromium Playwright shell/API/readiness smoke;
- Docker Compose build/start/readiness smoke.

### Phase 1A — merged server durability core

PR #6 is merged to `main` at `40053821aa470728083b5235070ad626d1c76be2`.

The server-side recording contract includes:

- durable PostgreSQL live-session lifecycle;
- explicit active writer ID and capture epoch;
- stale/competing writer checks;
- heartbeat-based interruption derivation without inferring successful completion;
- sequenced `/api/v1` binary chunk ingestion;
- per-chunk writer/epoch/timing/content/hash identity evidence;
- strict retry idempotency: a sequence can be retried only with matching accepted metadata and bytes;
- filesystem `AudioStorage` using temp write, flush/fsync, atomic replace, parent-directory fsync where available, and post-write integrity verification;
- PostgreSQL acceptance metadata committed before HTTP ACK;
- safe retry through the file-before-database crash window by reusing an identical orphaned durable file;
- persistent Compose audio volume;
- accepted sequence range/highest-contiguous reconciliation;
- immutable explicit sequence/wall-clock gap declarations;
- final sequence/high-water and monotonic-time boundaries;
- rejection of final boundaries that exclude accepted audio;
- finalization that remains incomplete while expected evidence is missing;
- retry-safe completed finalization tied to the finalizing writer/epoch;
- automated isolation of independent concurrent sessions.

### Phase 1B — browser recorder implemented and automated-CI proven on PR #7

The browser half now implements:

- microphone permission/capability handling and `MediaRecorder` capture;
- Dexie/IndexedDB recovery spool;
- atomic local commit of each captured fragment together with the local sequence/timing high-water, preventing a crash/race window that could overwrite an unacknowledged sequence;
- Web Crypto SHA-256 per fragment;
- delete-local-only-after-server-ACK behavior;
- bounded upload retry/backoff;
- reconnect/manual sync reconciliation against server accepted ranges;
- persistent-origin-storage request/status and quota safety reporting;
- same-origin capture coordination using Web Locks with a local-storage fallback;
- a transactional IndexedDB stale-writer fence as a final local guard against silent same-sequence overwrite;
- heartbeat/interruption/recovery UX;
- clean Stop that waits for MediaRecorder stop/final data, drains the local async persistence chain, flushes pending uploads, then finalizes the declared high-water mark;
- explicit unsafe/recoverable behavior when IndexedDB chunk persistence fails instead of continuing to claim safe recording;
- explicit wall-clock gap evidence for uncertain recovery intervals;
- responsive recorder UI with pending/synced/recoverable/unsafe state.

The latest exact-code automated witness before this documentation reconciliation is GitHub Actions run `34312039836` on commit `1f8d22d2f8711d6aa05dc90f87ff7d9120f49a6c`; all four CI jobs completed successfully.

Automated browser evidence includes:

- normal fake-microphone capture -> local spool -> durable ACK -> clean finalization;
- two independent browser contexts recording/finalizing concurrently without session mixing;
- temporary chunk-upload failure while capture continues into IndexedDB, followed by catch-up and clean finalization;
- page refresh with unacknowledged IndexedDB audio followed by recovery/finalization;
- disappearance of the recording tab without clean Stop followed by a recovery page, resumed capture, and finalization;
- rejection of a competing same-origin tab attempting to resume while the owning tab still holds the capture lock;
- a bounded Chromium background-tab interval while the browser process remains awake, followed by clean finalization;
- injected IndexedDB chunk-write failure causing capture to stop and surface an explicit unsafe/recoverable state rather than hiding the continuity uncertainty;
- storage-persistence granted/denied and low-quota safety-state unit tests;
- API process restart after an acknowledged audio chunk, followed by an idempotent retry that proves the durable audio file and PostgreSQL acceptance ledger survived the restart.

The automated background-tab test is **not** evidence that an operating-system-minimized Chrome/Edge window, sleeping laptop, closed browser, or mobile-backgrounded browser is universally reliable. A bounded real desktop witness is still required before Phase 1 is called complete.

## Remaining Phase 1 work before closing Issue #5

Phase 1 is not finished yet.

Two bounded items remain:

1. **Capture-generation ownership hardening.** The current recovery client deliberately reuses the same persistent writer ID/capture epoch after a page interruption so previously spooled chunks remain uploadable. That is workable for the current same-origin browser slice, but it does not yet fully satisfy ADR 0002's stronger distinction between uploading old recovery chunks and owning a newly started live capture generation. A follow-up must rotate/fence new capture ownership without making already-spooled recovery data unrecoverable, and must preserve retry-safe recovery semantics.
2. **Real desktop witness.** Run a bounded Chrome and/or Edge test on an awake desktop/laptop with the window backgrounded/minimized and normal application switching. Record exact OS/browser/version/duration and verify the final server/local evidence. CI cannot substitute for that platform witness.

Until item 1 is fixed, do not describe the capture epoch as a complete cross-tab/device takeover fence. Until item 2 is witnessed, describe automated background behavior only as the tested Chromium background-tab case.

Cross-device takeover remains intentionally out of scope for the initial web slice; future native/authenticated clients can add a stronger device/user ownership model on top of the same session/ingest contracts.

## Not implemented yet

- background/Celery processing workers;
- Groq or local STT;
- realtime transcript delivery;
- diarization;
- rolling/final summaries;
- tus upload pipeline;
- authentication/authorization;
- recording deletion/retention UI;
- production deployment/reverse proxy;
- native Android/iOS clients.

Do not describe those as working until repository evidence proves them.

## Immediate next delivery

GitHub Issue #5 remains the source of truth for **Phase 1: reliable desktop browser recording and recovery**.

After PR #7 is merged, the immediate implementation slice is the capture-generation ownership hardening described above, followed by the bounded real desktop Chrome/Edge witness.

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
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect degraded/unsafe recovery state.
- A currently open MediaRecorder fragment is not yet a durable IndexedDB/server chunk; abrupt process/device loss can therefore lose an un-emitted bounded tail even when all earlier emitted chunks are safe.
- WebSocket is for realtime updates, not durable state.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are a normal operating condition, not an edge case.
- A single live session has one active capture writer at a time.
- Current Phase 1B recovery ownership is same-origin/same-writer/same-epoch; stronger new-generation fencing is the next Phase 1 slice.
- Public upstream must remain free of deployment secrets and organization-private data.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
