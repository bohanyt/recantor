# Current

Last updated: 2026-09-10

This file is the short operational source of truth for the current Recantor state. Read it before planning implementation work, then verify GitHub fresh. Repository/PR/issue/CI state outranks this summary if the repository has moved since this file was written.

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

Not every selected component is implemented yet. Architecture invariants are defined in `docs/ARCHITECTURE.md`, `docs/decisions/0002-recording-access-guardrails.md`, and `AGENTS.md`.

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
- already-spooled evidence must not be silently destroyed when ownership rotates;
- Stop/finalize declares a final sequence/high-water mark;
- a server ACK requires durable audio plus durable acceptance metadata;
- raw MediaRecorder chunks are ordered media fragments and must not be assumed independently decodable;
- product API routes start under `/api/v1`;
- `/healthz` is process liveness and `/readyz` is dependency readiness;
- Live Intelligence must gain authentication/ownership before production exposure;
- recording deletion/retention and visible recording state are part of the production privacy baseline.

## Repository state

Canonical `main` at this checkpoint: `f989d88ec9765ae9678941ded11789ae810f6711`.

Post-merge CI run `34410387405` on that SHA completed successfully across the repository CI matrix.

### Phase 0 — complete

The runnable application foundation is implemented and CI-proven: React/TypeScript/Vite/Tailwind web shell, FastAPI/Pydantic API, PostgreSQL/SQLAlchemy/Alembic, Redis development/CI service, versioned `/api/v1`, generated OpenAPI client, frozen dependency installs, Docker Compose, and baseline browser/Compose smoke coverage.

### Phase 1A — merged server durability core

PR #6 is merged to `main` at `40053821aa470728083b5235070ad626d1c76be2`.

Implemented server behavior includes durable live-session lifecycle, capture writer/epoch fencing, sequenced binary ingestion, per-chunk timing/content/hash evidence, strict idempotent duplicate semantics, crash-safe filesystem media commit, PostgreSQL acceptance metadata before ACK, accepted-range/highest-contiguous reconciliation, explicit gaps, final high-water boundaries, and incomplete-finalization refusal while evidence is missing.

### Phase 1B — merged browser recovery recorder

PR #7 is merged to `main` at `7480fd203aba381896d7bd7cf4a3b41f5f40d144`.

Implemented browser behavior includes `MediaRecorder` capture, Dexie/IndexedDB recovery spool, atomic fragment + local sequence/timing high-water persistence, SHA-256 evidence, delete-local-only-after-durable-ACK behavior, bounded upload retry/backoff, reconnect/manual sync, storage safety reporting, same-origin capture coordination, recovery UX, final fragment flush on Stop, and explicit recoverable/unsafe states.

### Phase 1C — merged capture-generation fencing

PR #9 is merged to `main` at `e277535fca7bfcf5c046d79e07681b822e839398`.

The server/client contract distinguishes uploading old recovery evidence from ownership of a new capture generation. Recovery capabilities are high-entropy and stored server-side only as hashes. Each resumed generation uses a fresh writer and incremented epoch; claim retries are idempotent; old writers are fenced; pending claim intent is persisted; failed initial create identity is reusable; unexpected recorder/track failure retains emitted recovery evidence.

### Phase 1D — merged bounded recorder requests and escapable finalization

PR #17 is merged to `main` at `11221961f91a3db6681a5970becacb4ad62fa9f5`, closing #14.

Recorder HTTP requests now have configurable per-attempt deadlines (`VITE_RECORDING_REQUEST_TIMEOUT_MS`, default 5000 ms), explicit timeout/network/HTTP/caller-cancelled classification, bounded idempotent-safe upload retries, retained local evidence after exhausted retries, an enabled **Keep locally and finish later** action in `FINALIZING`, and convergence when the server committed `COMPLETE` but the browser lost the finalize response.

PR-head CI `34349870348` and post-merge `main` CI `34351060513` completed successfully.

### Phase 1E — merged deterministic clean Stop sync

PR #19 is merged to `main` at `50eaf450d4746569160876d81beb2f97c432c288`, closing #18.

After `MediaRecorder` stops and local persistence drains, Stop cancels/settles any pre-Stop single-flight sync and runs exactly one fresh bounded sync over the stable stopped spool before finalization. The retries-disabled regression first reproduced the stale-snapshot failure and then passed after the fix.

PR-head CI `34353847749` and post-merge `main` CI `34355423171` completed successfully.

### Phase 1F — merged fenced-capture orphan safety

PR #20 is merged to `main` at `f989d88ec9765ae9678941ded11789ae810f6711`, closing #11. Issue #12 was closed as completed after the merge because its bounded Phase 1 interim mitigation landed in the same PR.

Merged behavior:

- a server-authoritative stale-writer conflict from either heartbeat or chunk ingestion terminally fences the active browser generation;
- the browser stops `MediaRecorder`/microphone capture and releases the same-origin capture lock;
- emitted old-generation evidence is retained and surfaced as **orphaned local evidence — not safely syncable** rather than pending/retryable audio;
- stale Resume / Finish recovered / Sync / Start actions are withheld;
- reload checks server ownership truth before ordinary recovery reconciliation, including when the newer generation is already `COMPLETE`, so old-epoch orphan evidence is not erased;
- compact `accepted_ranges` may delete a local fragment only when the fragment's `captureEpoch` matches the server session's current `capture_epoch` in addition to sequence membership.

The last rule is explicitly an **interim Phase 1 safety mitigation**, not the final multi-client reconciliation identity contract. Hash-per-sequence responses, sequence-space partitioning, cross-device backlog semantics, and automatic reacquisition remain deferred.

The retries-disabled fenced-capture Playwright suite exercises heartbeat fencing, chunk fencing, capture/lock release, honest orphaned UI, and the executed B2→B3 composition where a newer generation accepts the same sequence with different bytes. The exact older local fragment survives completed takeover + reload. CI run `34359040563` passed with 20/20 E2E tests; final PR-head CI `34359711581` passed across backend, frontend, Chromium E2E, and Compose smoke. Post-merge `main` CI `34410387405` also completed successfully.

## Validated Phase 1 blocker status

An independent executed re-review at `e277535fca7bfcf5c046d79e07681b822e839398` originally reproduced five defects; final review later found #18. Their current status is:

| Issue | Status | Current truth |
| --- | --- | --- |
| #10 | **open blocker** | Liveness interruption is read-triggered and `interrupted_at` survives heartbeat restore. A stall can disappear if the next event is a heartbeat before any read observes it. |
| #11 | **fixed / closed by PR #20** | Fenced capture now stops, releases mic/lock, retains emitted evidence, and surfaces it as orphaned rather than safely retryable. |
| #12 | **fixed for Phase 1 / closed by PR #20** | Reconciliation now has the interim same-capture-epoch deletion guard; the final multi-client identity contract remains deferred. |
| #13 | **open blocker — next** | If an expected middle sequence is absent from the local spool, the browser cannot deliberately declare it as a gap and repeated Finish attempts can append useless wall-clock gaps. |
| #14 | **fixed / closed by PR #17** | Recorder requests are bounded and `FINALIZING` has a recoverable escape. |
| #18 | **fixed / closed by PR #19** | Clean Stop runs a fresh stable-spool sync instead of reusing a stale pre-Stop snapshot. |

Detailed executed evidence remains in the corresponding issues, PRs, and Issue #5 control-tower comments. Do not infer a natural browser cause for #13: the executed repro deliberately removed a middle IndexedDB record only to establish the missing-fragment precondition.

## Immediate Phase 1 order

GitHub Issue #5 remains the source of truth for **Phase 1: reliable desktop browser recording and recovery**.

Work in this order unless fresh evidence requires a change:

1. **#13** — explicit missing-sequence resolution and stable gap accounting.
2. **#10** — liveness/interruption semantics, without automatically converting a heartbeat/liveness hole into an audio gap.
3. **Real desktop Chrome/Edge witness** — awake Windows/desktop machine, real microphone capture, browser backgrounded/minimized for a bounded interval, return and Stop, then record exact OS/browser/version/duration plus final ACK/continuity evidence.
4. Close Issue #5 only after the code blockers and real-platform witness are satisfied.

Do not pull Groq, Whisper, diarization, or LLM summaries into Phase 1. The recording path must be trustworthy independently first.

### #13 guardrails

Issue #13 is the next implementation item. Required behavior includes:

- surface `missing_sequences` returned by incomplete finalize in recorder state/UI;
- offer an explicit, informed user action to declare the unresolved sequence(s) as gaps only **after retry/recovery has been offered**;
- never automatically turn a transient missing sequence into permanent declared loss;
- repeated failed Finish attempts must not keep appending wall-clock gaps;
- if the missing local fragment is available again, retry should upload it and complete normally without declaring a gap;
- add deterministic E2E coverage for declared-gap completion, late-fill completion, and stable gap count across repeated failure.

Do not broaden #13 into allowing `claim_capture` from `FINALIZING`; that lifecycle change remains explicitly undecided.

### #10 guardrails

Issue #10 follows #13. The bounded requirement is to detect a heartbeat stall before overwriting the old heartbeat timestamp, preserve unambiguous current-vs-historical liveness evidence, and make heartbeat/claim semantics consistent. **Liveness interruption is not automatically audio discontinuity.** Locally retained audio may later prove continuity.

## What automated background-tab tests do not prove

The currently pinned Playwright environment uses `@playwright/test` 1.63.0. In an executed validation run, the browser launch included:

```text
--disable-background-timer-throttling
--disable-backgrounding-occluded-windows
--disable-renderer-backgrounding
--headless
```

Therefore automated background-tab coverage is not evidence for real OS-minimized Chrome/Edge behavior, laptop sleep, browser termination, or mobile background suspension. The manual/real desktop witness remains a Phase 1 exit requirement.

## Phase 2 / downstream gate

Issue #15 is **not a Phase 1 blocker**. It is the gate before the first downstream STT/summary consumer.

Today `COMPLETE` does not distinguish, from the session resource alone, among fully durable audio, partial/gapped audio, and zero-audio completion. #15 requires a persisted terminal completeness/continuity classification before downstream consumers are written. #13 should settle first or concurrently because its declared-gap completion path defines the partial/gapped case.

After Phase 1 closes, resolve #15 before implementing the first actual STT consumer.

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

## Open decisions intentionally deferred

Resolve these by evidence/ADR when their implementation phase begins:

- exact authentication implementation / OIDC or local-account strategy;
- exact LLM provider(s) for rolling summaries;
- final live/offline diarization backends;
- speaker embedding model and confidence calibration;
- production object-storage backend;
- exact queue concurrency/routing values;
- exact VAD/utterance timing after benchmark;
- exact mobile native framework;
- downstream organization-specific branding and infrastructure;
- whether a server-side liveness stall implies any audio discontinuity (#10);
- how a fenced or offline client may deliver a backlog under a new generation (#11 follow-on / future ADR);
- the proper multi-client reconciliation identity contract beyond #12's interim epoch guard;
- terminal-state vocabulary for clean / partial-gapped / empty results (#15);
- which real-world mechanisms can leave an expected middle fragment absent from the local spool (#13 does not establish one);
- whether `claim_capture` should ever accept a non-complete `FINALIZING` session.

## Known design boundaries

- Browser background/minimized recording on an awake desktop remains a core web use case requiring a real-platform witness.
- Closing the browser, sleeping/shutting down the computer, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect degraded/unsafe recovery state.
- A currently open MediaRecorder fragment is not yet durable IndexedDB/server evidence; abrupt process/device loss can lose an un-emitted bounded tail even when earlier emitted fragments are safe.
- A server ACK is the strong durability boundary.
- WebSocket is for realtime updates, not durable truth.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are normal.
- A single live session has one active capture generation at a time, fenced by writer identity + epoch.
- Fenced old-generation evidence is retained locally but currently has no operator recovery/export/expiry flow; #11 intentionally did not decide one.
- #12's same-epoch reconciliation rule is an interim safety guard, not a future multi-device contract.
- Cross-device takeover/backlog semantics are not claimed.
- Public upstream must remain free of deployment secrets and organization-private data.

## Local development notes

- `apps/api/tests/conftest.py` drops the entire schema on teardown. Point `DATABASE_URL` at a dedicated test database; running `pytest` against a database an API process is using destroys recording tables while leaving `alembic_version` at head, so a subsequent `alembic upgrade head` is a no-op and the API 500s.
- `apps/web/openapi-ts.config.ts` defaults to `http://localhost:8000`. On IPv6-first hosts this can fail against an API bound to `127.0.0.1`; set `OPENAPI_INPUT` explicitly.

## Handoff rule

Control-tower work is intentionally transferable between chats. GitHub is the technical source of truth; chat history is convenience only.

For a new control-tower chat:

1. inspect GitHub fresh;
2. read `AGENTS.md`;
3. read this file;
4. read the latest dated file under `docs/handoff/`;
5. read Issue #5 and the current next blocker/PR;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.