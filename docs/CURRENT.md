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
- Node.js 24 LTS for web/tooling and Python 3.13 for the main API.

Not every selected component is implemented yet. The repository state below distinguishes proven runtime from planned downstream stack.

Architecture invariants are defined in `docs/ARCHITECTURE.md`, `docs/decisions/0002-recording-access-guardrails.md`, and `AGENTS.md`.

Most important:

> Capture is infrastructure. Intelligence is downstream.

The implemented browser/server durability path is:

```text
MediaRecorder
  -> atomic Dexie/IndexedDB fragment + local high-water commit
  -> sequenced HTTP upload with hash/timing/ownership evidence
  -> crash-safe filesystem audio commit + PostgreSQL acceptance metadata
  -> durable HTTP ACK
  -> local fragment deletion
```

STT, diarization, live transcript delivery, and LLM summaries remain downstream and are intentionally absent from Phase 1.

Non-optional recording guardrails:

- browser IndexedDB is a recovery spool, not the final durability boundary;
- only one active capture writer may own a live session at once;
- a resumed `MediaRecorder` instance is a new capture generation with a fresh writer and fenced epoch;
- already-spooled evidence remains recoverable after ownership rotates;
- Stop/finalize declares a final sequence/high-water mark;
- a server ACK requires durable audio plus durable acceptance metadata;
- raw MediaRecorder chunks are ordered media fragments and must not be assumed independently decodable;
- product API routes start under `/api/v1`;
- `/healthz` is process liveness and `/readyz` is dependency readiness;
- Live Intelligence must gain authentication/ownership before production exposure;
- recording deletion/retention and visible recording state are part of the production privacy baseline.

## Repository state

### Phase 0 — complete

The runnable application foundation is implemented and CI-proven:

- React + TypeScript + Vite + Tailwind web shell;
- TanStack Query server-state usage;
- FastAPI/Pydantic API;
- `/healthz` and dependency-aware `/readyz`;
- versioned `/api/v1` product namespace;
- OpenAPI-generated TypeScript API client contract;
- PostgreSQL + SQLAlchemy 2 + Alembic;
- Redis development/CI service;
- frozen `uv` and `pnpm` dependency installs;
- Docker Compose development stack;
- backend/frontend quality gates;
- Chromium Playwright shell/API smoke;
- Docker Compose build/start/readiness smoke.

### Phase 1A — merged server durability core

PR #6 is merged to `main` at `40053821aa470728083b5235070ad626d1c76be2`.

The server-side contract includes:

- durable live-session lifecycle and explicit session kind;
- active writer/capture epoch;
- heartbeat-derived interruption without inferring completion;
- sequenced binary chunk ingestion under `/api/v1`;
- per-chunk writer/epoch/timing/content/hash evidence;
- strict idempotent duplicate semantics;
- crash-safe filesystem `AudioStorage` commit and verification;
- PostgreSQL acceptance metadata before HTTP ACK;
- file-before-database crash-window reconciliation;
- accepted-range/highest-contiguous reconciliation;
- immutable explicit sequence/wall-clock gaps;
- final high-water/monotonic boundaries;
- incomplete finalization while expected evidence is missing;
- retry-safe completed finalization;
- automated concurrent-session isolation.

### Phase 1B — merged browser recovery recorder

PR #7 is merged to `main` at `7480fd203aba381896d7bd7cf4a3b41f5f40d144`.

The browser recorder includes:

- microphone permission/capability handling and `MediaRecorder` capture;
- Dexie/IndexedDB recovery spool;
- atomic local fragment + sequence/timing high-water commit;
- Web Crypto SHA-256 per emitted fragment;
- delete-local-only-after-server-ACK behavior;
- bounded upload retry/backoff and manual/reconnect sync;
- server/client accepted-sequence reconciliation;
- persistent-origin-storage/quota safety reporting;
- Web Locks with local-storage fallback for same-origin capture coordination;
- transactional IndexedDB stale-sequence protection;
- heartbeat/interruption/recovery UX;
- clean Stop that waits for final MediaRecorder data, drains local persistence, flushes pending upload, then finalizes;
- unsafe/recoverable behavior on local spool failure;
- explicit wall-clock gap evidence for uncertain intervals;
- responsive durability/pending/sync/recovery UI.

### Phase 1C — implemented and final-review CI proven on PR #9

PR #9 implements the ownership distinction required by ADR 0002 between **uploading old recovery evidence** and **owning a newly started live capture generation**.

The implemented contract is:

- new live sessions receive a high-entropy recovery capability;
- the server stores only its SHA-256 hash; the raw capability stays client-side;
- each resumed `MediaRecorder` generation uses a fresh writer ID;
- the next generation cannot start until previously spooled fragments have been reconciled and durably ACKed;
- a valid recovery claim increments the capture epoch exactly once;
- an exact retry after a lost successful claim response is idempotent and does not increment the epoch again;
- after takeover, heartbeat and new chunk ingestion from the old writer/epoch are rejected;
- an already-accepted old fragment may still be retried idempotently and integrity-checked after takeover;
- browser claim intent (`pendingWriterId`) is persisted before the claim so lost-response recovery can reconcile server truth;
- initial create identity (`clientRequestId`, initial writer, recovery capability) is also persisted so a failed create request can be retried rather than silently creating a different session;
- `finishRecovered` finalizes existing evidence without creating a new capture generation, except that it can reconcile a generation claim that already succeeded but whose response was lost;
- immediate same-browser recovery does not need to wait for heartbeat expiry before fencing the old generation;
- unexpected `MediaRecorder` errors or microphone-track termination stop active capture, retain emitted recovery evidence, and move to an explicit recoverable/interruption path.

Final review found and fixed one real UX/retry bug: an initial create/start failure correctly retained its pending start identity but left the UI/controller in an `error` state from which Start could not be retried without a reload. Start is now explicitly retryable from that state, and a Playwright regression test proves the first create request can fail and the second attempt starts/finalizes using the persisted retry path.

The exact pre-documentation source witness is GitHub Actions run `34316307790` on commit `7533f8a719d30e324009d572d2c0ad36c0127e42`; backend, frontend, Chromium e2e, and Compose smoke all completed successfully.

Automated evidence now covers, among other cases:

- normal fake-microphone capture -> local spool -> durable ACK -> clean finalization;
- concurrent independent browser contexts without session mixing;
- temporary upload loss with local accumulation and later catch-up;
- refresh/reopen recovery of unacknowledged IndexedDB evidence;
- recording-tab disappearance followed by a fresh writer/capture generation;
- lost successful generation-claim response followed by an idempotent retry;
- refusal to start a new generation while old recovery fragments remain unacknowledged;
- stale old-writer heartbeat/new-chunk rejection after takeover;
- duplicate retry of already-accepted old evidence after takeover;
- competing same-origin tab protection;
- IndexedDB write failure -> explicit unsafe/recoverable behavior;
- unexpected microphone-track end -> recoverable state and explicit continuity evidence;
- initial session-create/start failure -> user-visible retry -> successful recording/finalization;
- API process restart after durable ACK -> idempotent retry with filesystem/PostgreSQL evidence intact;
- bounded Chromium background-tab capture while the browser process remains awake.

The automated background-tab test is **not** evidence that an operating-system-minimized Chrome/Edge window, sleeping laptop, closed browser, or mobile-backgrounded browser is universally reliable.

## Remaining Phase 1 work before closing Issue #5

Phase 1 is not complete yet, but only one bounded exit item remains:

**Real desktop background/minimized witness.** Run Chrome and/or Edge on an awake desktop/laptop, start a real microphone recording, background/minimize the browser while switching among ordinary applications for a bounded interval, then return and Stop. Record the exact OS, browser/version, duration, and final continuity/ACK evidence.

CI cannot substitute for that platform/lifecycle witness. Until it is recorded, describe automated background behavior only as the tested Chromium background-tab case.

Cross-device capture takeover remains intentionally out of scope for the initial web slice. Future authenticated/native clients can add a stronger device/user ownership model on top of the same session/ingest contracts.

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

After PR #9 merges, the immediate next action is the bounded real Chrome/Edge background/minimized witness on an awake desktop/laptop. If that witness passes and its evidence is recorded, Phase 1 can be closed before starting downstream STT work.

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

- Browser background/minimized recording on an awake desktop is a core web use case still requiring a real-platform witness.
- Closing the browser, sleeping/shutting down the computer, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect degraded/unsafe recovery state.
- A currently open MediaRecorder fragment is not yet a durable IndexedDB/server chunk; abrupt process/device loss can lose an un-emitted bounded tail even when all earlier emitted fragments are safe.
- A server ACK is the strong durability boundary.
- WebSocket is for realtime updates, not durable state.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are a normal operating condition.
- A single live session has one active capture generation at a time; each resumed generation is fenced by fresh writer identity + incremented epoch.
- Old already-spooled/accepted evidence remains recoverable without granting authority to create new stale-writer evidence.
- Cross-device takeover is not yet claimed.
- Public upstream must remain free of deployment secrets and organization-private data.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
