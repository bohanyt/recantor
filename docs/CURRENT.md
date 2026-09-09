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

Several of these guardrails are stated as intent but are **not currently upheld by the implementation**. See "Validated Phase 1 blockers" below before relying on any of them.

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

### Phase 1C — merged capture-generation fencing

PR #9 is merged to `main` at `e277535fca7bfcf5c046d79e07681b822e839398`.

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

### What the automated background-tab test does not prove

This is an observation about the tooling **as currently pinned and as executed in one validation run**, not a timeless property of Playwright.

`apps/web/pnpm-lock.yaml` currently pins `@playwright/test` **1.63.0**. In an executed validation run using that pinned version, the launched browser process was inspected via `/proc/<pid>/cmdline` and its argument list included:

```text
--disable-background-timer-throttling
--disable-backgrounding-occluded-windows
--disable-renderer-backgrounding
--headless
```

In that run those flags came from Playwright's own default launch arguments, not from `apps/web/playwright.config.ts`, which contributes only the fake-media flags. (The run drove a Chromium 141 build via an `executablePath` override because the CI-pinned browser download was unavailable in the review environment; the flags above originate from the Playwright launcher rather than from the browser build.)

Consequently, in the tooling as pinned today, the `continues through a bounded Chromium background-tab interval` test runs headless in a browser where the three throttling mechanisms it would need to exercise are switched off, and it backgrounds a tab with `bringToFront()` rather than minimizing an operating-system window.

If the pinned Playwright version changes, re-inspect the launch arguments rather than assuming this still holds. Note also that the flags being absent would not by itself turn this test into a valid witness: it would still be headless and still be occluding a tab rather than minimizing a window.

As it stands, it is **not** evidence about real Chrome/Edge throttling, an OS-minimized window, a sleeping laptop, a closed browser, or a mobile-backgrounded browser. Do not cite it as such.

## Validated Phase 1 blockers

An independent executed re-review at `e277535fca7bfcf5c046d79e07681b822e839398` reproduced five defects at runtime, using the repository's own toolchain (backend `pytest`, `vitest`, and Playwright with retries disabled) against a real API, PostgreSQL, and Chromium. Final review of the #14 remediation later exposed one additional timing-dependent Stop/sync race (#18). These are product gaps, not reasons to weaken the durability boundary.

**Phase 1 cannot close until these are fixed.**

| Issue | Defect | Executed evidence |
| --- | --- | --- |
| #10 | Liveness interruption is detected only when something reads the session, and `interrupted_at` is never cleared on heartbeat restore | a 10-minute stall left `state: recording`, `interrupted_at: None`, no gaps; a stall that was read left `interrupted_at` populated permanently through restore |
| #11 | A fenced writer keeps capturing audio the server will never accept, and the UI presents it optimistically | after an external takeover the page stayed in `phase: recording` with `pending: 3 fragments`, message `capture continues into the recovery spool` |
| #12 | Reconciliation deletes local audio on accepted-sequence membership alone | a real 32,860-byte local fragment was deleted because another generation had accepted that sequence number with different bytes |
| #13 | When an expected middle fragment is absent from the local spool, the session is stranded in `FINALIZING` with no user path out | with one middle spool fragment **deliberately removed** so it was absent at finalization: Resume refused (`session in state finalizing cannot be claimed`), Finish looped identically 4x, every finalize sent `gap_sequences: []`, and each retry appended another wall-clock gap |
| #14 | A stalled chunk upload hangs Stop in `FINALIZING` permanently | after 30s: `phase: finalizing`, controls `{start:0, stop:0, resume:0, finish:0, syncDisabled:true}`, server `final_sequence: null` |
| #18 | Stop can reuse a pre-Stop single-flight sync snapshot and miss the final persisted fragment in the pass it awaits | run `34348856554` first attempt returned `Capture stopped, but some audio still needs a server ACK`; the Playwright retry passed, proving a timing-dependent non-lossy race |

On #13, note carefully what is and is not established. The **product behavior** is proven: given a spool that is missing an expected middle sequence at finalization, the recorder cannot reach any terminal state. The **cause** of such partial spool loss is not established — the fragment in the repro was removed deliberately to create the precondition. Do not read #13 as evidence that browsers evict individual IndexedDB records, or as a characterization of any specific browser storage behavior.

Consequences for statements elsewhere in this file:

- "Browser disappearance without a clean Stop creates an interruption" is only true if something reads the session during the interruption (#10).
- "Already-spooled evidence remains recoverable after ownership rotates" does not currently hold for evidence produced by a fenced generation (#11, #12).
- "A session may become complete only after every expected sequence is either durably present or explicitly represented as a gap" is upheld by the server, but the browser has no path to declare the gap, so affected sessions never reach a terminal state at all (#13).
- A clean Stop is not yet deterministic if it reuses an in-flight sync snapshot created before the final fragment was persisted (#18); the audio remains local, but the UI can unnecessarily fall back to recovery.

PR #17 implements the bounded #14 remediation on branch `phase1/b5-bounded-finalization`. Recorder HTTP attempts now have a configurable per-attempt deadline (`VITE_RECORDING_REQUEST_TIMEOUT_MS`, default 5000 ms), with invalid/sub-millisecond configured values falling back rather than becoming a zero-millisecond deadline. Timeout remains retryable inside the existing bounded upload retry loop; exhausted attempts retain local audio and return Stop to `recoverable`; and `FINALIZING` exposes an enabled **Keep locally and finish later** escape that cancels in-flight recorder requests without discarding emitted evidence. The #14-specific Playwright file forces `retries: 0`, covers a request that never settles during Stop, covers a hung ordinary-recording upload that later catches up, and covers a lost successful finalize response by reconciling matching remote `COMPLETE` truth instead of wedging recovery. GitHub Actions run `34348856554` on head `fe72d4e908729ddc36ed9cfc9fbb04aa43fe6ab2` completed successfully across backend, frontend, Chromium e2e, and Compose smoke. The same run exposed #18 as a first-attempt flake in an older recovery-generation test; its retry passed. This is PR evidence until #17 merges; do not describe #14 as merged before then.

Remaining blocker order after #17: #18 first, then #11 with #12 alongside it, then #13, then #10. Fixes belong in separate bounded PRs unless a proven dependency makes a coordinated slice clearer.

## Phase 2 / downstream gate

Issue #15 records that a terminal `COMPLETE` session carries no classification distinguishing fully durable audio, partially gapped audio, and no audio at all. A session with zero durable fragments and zero gaps is `COMPLETE` today, and the recorder reports that every expected sequence was durably acknowledged.

This is **not a current runtime failure**: no STT, diarization, summary, or Celery consumer exists yet, so nothing is presently making a wrong decision on this data. It is a contract gap that must be closed before the first downstream consumer is written, and it gates the start of Phase 2 rather than the close of Phase 1.

## Remaining Phase 1 work before closing Issue #5

Two things remain, in order:

1. **Merge the proven #14 remediation, then fix the five remaining validated blockers** (#18, #10, #11, #12, #13), each with regression coverage that the executed repro no longer reproduces.
2. **Real desktop background/minimized witness.** Run Chrome and/or Edge on an awake desktop/laptop, start a real microphone recording, background/minimize the browser while switching among ordinary applications for a bounded interval, then return and Stop. Record the exact OS, browser/version, duration, and final continuity/ACK evidence.

The witness remains a required Phase 1 exit item, but it should be recorded **after** the blockers are fixed. After #14, the fenced-writer/reconciliation defects (#11/#12) and the clean-Stop sync race (#18) remain capable of distorting what a witness appears to show.

CI cannot substitute for that platform/lifecycle witness. Until it is recorded, describe automated background behavior only as the tested headless Chromium background-tab case, with the tooling caveat above.

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

GitHub Issue #5 remains the source of truth for **Phase 1: reliable desktop browser recording and recovery**. Issues #10-#14 and #18 are its blocking work items; #10-#14 are attached sub-issues, while #18 is linked from #5 because the available connector did not expose sub-issue mutation.

PR #17 is the bounded #14 remediation and must be reviewed/merged before #14 is considered closed. After that, #18 is the immediate bounded fix because it depends on #17's deadline/cancellation primitives and closes the now-executed clean-Stop sync race. Then continue with #11 plus the #12 interim reconciliation guard.

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
- downstream organization-specific branding and infrastructure;
- **whether a server-side liveness stall implies audio discontinuity.** Liveness interruption and proven audio discontinuity are not the same claim; a client may hold the audio for a stalled interval locally and later prove continuity by delivering it. Do not assume a heartbeat stall should create an audio gap (#10);
- **how a fenced or offline client may deliver a backlog under a new capture generation.** Any per-epoch sequence-space partition or equivalent cross-device backlog scheme is a multi-device ownership contract change and needs its own ADR before native/multi-device clients (#11);
- **the multi-client reconciliation identity contract** — what per-sequence acceptance identity `/recording-state` should expose, and how that interacts with range compression. The Phase 1 fix for #12 is an interim epoch guard, not this contract;
- **the terminal-state vocabulary for completeness classification**, including whether a zero-audio session belongs in `COMPLETE` at all (#15);
- **which real-world mechanisms can leave an expected middle fragment absent from the local spool.** #13 proves the product dead-end given that precondition, but does not establish how the precondition arises. Characterizing browser storage eviction, partial corruption, or other causes needs its own investigation before any claim is made about likelihood.

## Known design boundaries

- Browser background/minimized recording on an awake desktop is a core web use case still requiring a real-platform witness.
- With the currently pinned Playwright version, the launched Chromium disables background throttling, so the automated background-tab test cannot stand in for that witness. Re-inspect if the pin changes.
- Closing the browser, sleeping/shutting down the computer, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect degraded/unsafe recovery state.
- If an expected middle fragment is absent from the local spool at finalization, the product currently has no path to any terminal state (#13). That product behavior is proven; the browser-specific mechanisms by which such partial spool loss might naturally occur are not established and were not observed.
- A currently open MediaRecorder fragment is not yet a durable IndexedDB/server chunk; abrupt process/device loss can lose an un-emitted bounded tail even when all earlier emitted fragments are safe.
- A server ACK is the strong durability boundary.
- WebSocket is for realtime updates, not durable state.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are a normal operating condition.
- A single live session has one active capture generation at a time; each resumed generation is fenced by fresh writer identity + incremented epoch.
- Fencing is enforced server-side, but the fenced client does not currently stop capturing (#11).
- A stopped recorder can still reuse a stale in-flight sync snapshot and leave its final emitted fragment pending locally (#18); this is recoverable and non-lossy but not yet a deterministic clean Stop.
- Old already-spooled/accepted evidence remains recoverable without granting authority to create new stale-writer evidence.
- Cross-device takeover is not yet claimed.
- Public upstream must remain free of deployment secrets and organization-private data.

## Local development notes

- `apps/api/tests/conftest.py` drops the entire schema on teardown. Point `DATABASE_URL` at a dedicated test database; running `pytest` against a database an API process is using destroys `recording_sessions`, `recording_chunks`, and `recording_gaps` while leaving `alembic_version` at head, so a subsequent `alembic upgrade head` is a no-op and the API 500s.
- `apps/web/openapi-ts.config.ts` defaults to `http://localhost:8000`. On IPv6-first hosts this fails against an API bound to `127.0.0.1`; set `OPENAPI_INPUT` explicitly.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
