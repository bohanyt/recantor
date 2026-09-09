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

Selected foundation:

- React + TypeScript + Vite + Tailwind CSS web SPA;
- TanStack Query for server state;
- Python + FastAPI + Pydantic API;
- PostgreSQL + SQLAlchemy 2 + Alembic durable structured state;
- Celery + Redis background processing;
- Dexie/IndexedDB browser recovery spool;
- Uppy + tus/tusd resumable existing-recording upload;
- Groq Whisper API as primary STT and faster-whisper/CTranslate2 as local fallback later;
- FFmpeg for media normalization;
- Docker Compose deployment baseline;
- Caddy as the default/simple production reverse proxy;
- Node.js 24 LTS and Python 3.13.

Not every selected component is implemented. Architecture invariants are defined in `docs/ARCHITECTURE.md`, `docs/decisions/0002-recording-access-guardrails.md`, and `AGENTS.md`.

Most important:

> Capture is infrastructure. Intelligence is downstream.

Implemented durability path:

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
- a resumed `MediaRecorder` is a new capture generation with a fresh writer and fenced epoch;
- already-spooled evidence must not be silently destroyed when ownership rotates;
- Stop/finalize declares a final sequence/high-water boundary;
- every expected sequence through that boundary must be durably present or explicitly represented as loss before completion;
- a server ACK requires durable audio plus durable acceptance metadata;
- raw MediaRecorder chunks are ordered media fragments and must not be assumed independently decodable;
- WebSocket/Redis are not durable source of truth;
- raw audio is not stored as PostgreSQL blobs.

## Repository state

Canonical `main` at this checkpoint: `791d6492a9d484c3e144748a74b9c92a31199d1b`.

PR #27 was deliberately squash-merged into `main` on 2026-09-10, closing Issue #13. Post-merge CI run `34416444878` completed successfully across backend, frontend, Chromium E2E, and Compose smoke.

### Merged Phase 1 deliveries

- **PR #6** — server durability core: durable live-session lifecycle, writer/epoch fencing, sequenced binary ingest, crash-safe filesystem commit, PostgreSQL acceptance metadata, accepted-range reconciliation, explicit gaps, and final high-water enforcement.
- **PR #7** — browser recovery recorder: MediaRecorder capture, Dexie/IndexedDB spool, atomic local high-water persistence, SHA-256 evidence, bounded retry/backoff, reconnect/manual sync, storage-safety reporting, recovery UX, and final-fragment flush.
- **PR #9** — capture-generation fencing: recovery capability, fresh writer/epoch on resume, stale-writer rejection, persisted pending claim intent, and unexpected-capture-failure recovery.
- **PR #17 / Issue #14** — bounded recorder requests and escapable finalization.
- **PR #19 / Issue #18** — deterministic clean Stop sync over a stable stopped spool.
- **PR #20 / Issues #11 and #12** — terminal browser fencing for stale writers plus the bounded same-epoch reconciliation safety guard. The final multi-client identity contract remains deferred.
- **PR #27 / Issue #13** — explicit missing-sequence resolution: unresolved sequences are visible, retry remains non-destructive, permanent loss requires an explicit user action, repeated failed Finish attempts do not amplify wall-clock gaps, and a late local fragment can still fill the hole normally. Deterministic E2E covers declared-gap completion, late-fill completion, and stable gap count.

## Validated Phase 1 blocker status

| Issue | Status | Current truth |
| --- | --- | --- |
| #10 | **open blocker — next** | Liveness interruption is read-triggered and `interrupted_at` survives heartbeat restore. A stall can disappear if the next event is a heartbeat before any read observes it. |
| #11 | **fixed / closed** | Stale-writer conflicts terminally fence active browser capture and retain orphaned evidence honestly. |
| #12 | **fixed for Phase 1 / closed** | Reconciliation has the interim same-capture-epoch deletion guard; final multi-client identity remains deferred. |
| #13 | **fixed / closed by PR #27** | Missing-sequence finalization now has explicit declared-gap and late-fill exits with stable retry accounting. |
| #14 | **fixed / closed** | Recorder requests are bounded and finalization can be deferred safely. |
| #18 | **fixed / closed** | Clean Stop performs a fresh stable-spool sync before finalization. |

Issue #5 remains open as the umbrella for **Phase 1: reliable desktop browser recording and recovery**.

## Immediate Phase 1 order

1. **Issue #10 — liveness/interruption semantics.**
2. **Real desktop Chrome/Edge witness** on an awake Windows/desktop machine using a real microphone, with the browser backgrounded/minimized for a bounded interval, then returned and stopped. Record exact OS/browser/version/test duration plus final ACK/continuity evidence.
3. Reconcile Issue #5 against its acceptance matrix and close it only when #10 and the real-platform witness are satisfied.
4. After Phase 1 closes, resolve **Issue #15** before writing the first downstream STT/summary consumer.

Do not pull Groq, Whisper, diarization, or LLM summaries into Phase 1.

## Issue #10 bounded contract

Current code has one `interrupted_at` field, while `_mark_interrupted_if_stale()` only runs on selected read/claim paths. `heartbeat()` overwrites `last_heartbeat_at` before preserving evidence of a stall and restores `INTERRUPTED -> RECORDING` without clearing `interrupted_at`.

The bounded fix must:

- detect a heartbeat stall exceeding `recording_heartbeat_timeout_seconds` before the next valid heartbeat overwrites the old timestamp;
- preserve unambiguous distinction between **currently interrupted** and **has experienced a liveness interruption historically**;
- ensure a restored session no longer reports itself as currently interrupted;
- make heartbeat and claim semantics consistent, with any intentional difference documented;
- cover stall → read → heartbeat, stall → heartbeat with no intervening read, and stall → claim;
- update `docs/CURRENT.md` with the resulting guarantee.

Important boundary:

> A liveness interruption is not automatically an audio gap.

The browser may still hold that interval in its local recovery spool and later prove continuity. Do not manufacture `RecordingGap` evidence merely because heartbeat delivery stalled. A background sweep/job is also not required by #10 if write-path detection satisfies the contract.

## What automated background-tab tests do not prove

The pinned Playwright environment disables normal background throttling and runs headless. Automated browser tests therefore do **not** prove real OS-minimized Chrome/Edge behavior, laptop sleep, browser termination, or mobile background suspension. The real desktop witness remains a Phase 1 exit requirement.

## Phase 2 / downstream gate

Issue #15 is not a Phase 1 blocker, but it must land before the first actual STT/summary consumer. Today `COMPLETE` alone does not distinguish fully durable audio, partial/gapped audio, and zero-audio completion. #15 must persist a terminal completeness/continuity classification so downstream intelligence does not infer more than capture evidence proves.

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

- exact authentication implementation;
- exact LLM provider(s) for rolling summaries;
- final live/offline diarization backends and speaker embedding model;
- production object storage and queue concurrency/routing;
- exact VAD/utterance timing;
- exact native mobile framework;
- whether a server-side liveness stall implies any audio discontinuity (#10; currently explicitly **not assumed**);
- how a fenced/offline client may deliver backlog under a new generation;
- the final multi-client reconciliation identity contract beyond #12's interim epoch guard;
- terminal-state vocabulary for clean / partial-gapped / empty results (#15);
- which real-world mechanisms can leave an expected middle fragment absent from the local spool;
- whether `claim_capture` should ever accept a non-complete `FINALIZING` session.

## Known design boundaries

- Browser background/minimized recording on an awake desktop is a core web use case requiring a real-platform witness.
- Closing the browser, sleeping/shutting down the computer, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect degraded/unsafe recovery state.
- An open MediaRecorder fragment is not yet durable IndexedDB/server evidence; abrupt process/device loss can lose an un-emitted bounded tail even when earlier emitted fragments are safe.
- Multiple simultaneous sessions are normal; one live session has one active capture generation at a time.
- Fenced old-generation evidence is retained locally but currently has no operator recovery/export/expiry flow.
- Cross-device takeover/backlog semantics are not claimed.
- Public upstream must remain free of deployment secrets and organization-private data.

## Local development notes

- `apps/api/tests/conftest.py` drops the entire schema on teardown. Use a dedicated test database; do not point pytest at a database an API process is using.
- `apps/web/openapi-ts.config.ts` defaults to `http://localhost:8000`. On IPv6-first hosts, set `OPENAPI_INPUT` explicitly if the API is bound only to `127.0.0.1`.

## Handoff rule

Control-tower work is intentionally transferable between chats. GitHub is technical source of truth; chat history is convenience only.

For a new control-tower chat:

1. inspect GitHub fresh;
2. read `AGENTS.md`;
3. read this file;
4. read the latest dated file under `docs/handoff/`;
5. read Issue #5 and the current next blocker/PR;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
