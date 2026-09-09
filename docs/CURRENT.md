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

Several guardrails are still blocked by validated defects. See **Validated Phase 1 blockers** before relying on them.

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
- initial create identity (`clientRequestId`, initial writer, recovery capability) is persisted so a failed create request can be retried rather than silently creating a different session;
- `finishRecovered` finalizes existing evidence without creating a new capture generation, except that it can reconcile a generation claim that already succeeded but whose response was lost;
- immediate same-browser recovery does not need to wait for heartbeat expiry before fencing the old generation;
- unexpected `MediaRecorder` errors or microphone-track termination stop active capture, retain emitted recovery evidence, and move to an explicit recoverable/interruption path.

Final review also fixed an initial create/start retry UX bug: persisted start identity is now reusable from the `error` state without requiring a reload.

### Phase 1D — merged bounded recorder requests and escapable finalization

PR #17 is merged to `main` at `11221961f91a3db6681a5970becacb4ad62fa9f5`, closing #14.

The merged browser behavior includes:

- configurable per-attempt recorder HTTP deadlines via `VITE_RECORDING_REQUEST_TIMEOUT_MS` (default 5000 ms);
- explicit timeout/network/HTTP/caller-cancelled failure classification;
- bounded idempotent-safe upload retries;
- retained local evidence when retries/deadlines are exhausted;
- an enabled **Keep locally and finish later** escape while `FINALIZING`;
- recovery convergence when the server committed `COMPLETE` but the browser lost or timed out the finalize response.

The final PR-head CI run `34349870348` was green across backend, frontend, Chromium e2e, and Compose smoke with 17/17 E2E tests passing. Post-merge `main` CI run `34351060513` also completed successfully.

### Phase 1E — merged deterministic clean Stop sync

PR #19 is merged to `main` at `50eaf450d4746569160876d81beb2f97c432c288`, closing #18. After `MediaRecorder` stops and local persistence drains, Stop now cancels/settles any pre-Stop sync and runs exactly one fresh bounded sync over the stable stopped spool before finalization.

The retries-disabled regression first reproduced the stale-snapshot failure, then passed after the fix. PR-head CI run `34353847749` and post-merge `main` CI run `34355423171` both completed successfully.

## What the automated background-tab test does not prove

This is an observation about the tooling **as currently pinned and as executed in one validation run**, not a timeless property of Playwright.

`apps/web/pnpm-lock.yaml` currently pins `@playwright/test` **1.63.0**. In an executed validation run using that pinned version, the launched browser process was inspected and its argument list included:

```text
--disable-background-timer-throttling
--disable-backgrounding-occluded-windows
--disable-renderer-backgrounding
--headless
```

Those flags came from Playwright's default launch arguments rather than `apps/web/playwright.config.ts`. The test also backgrounds a tab rather than minimizing an operating-system window.

Therefore the automated background-tab test is **not** evidence about real Chrome/Edge throttling, an OS-minimized window, a sleeping laptop, a closed browser, or a mobile-backgrounded browser. If the pinned Playwright version changes, re-inspect the launch behavior rather than assuming this still holds.

## Validated Phase 1 blockers

An independent executed re-review at `e277535fca7bfcf5c046d79e07681b822e839398` reproduced five defects at runtime. Final review of the #14 remediation later exposed the additional timing-dependent Stop/sync race tracked as #18.

| Issue | Current status | Defect / evidence |
| --- | --- | --- |
| #10 | **open blocker** | Liveness interruption is detected only when something reads the session, and `interrupted_at` survives heartbeat restore. A 10-minute stall with no intervening read could be overwritten by the next heartbeat without any interruption evidence. |
| #11 | **remediation in PR #20; not merged yet** | A fenced writer currently keeps capturing after takeover; PR #20 stops the stale generation, releases capture ownership, retains emitted local evidence as explicitly orphaned, and withholds stale recovery/sync actions. |
| #12 | **interim remediation in PR #20; not merged yet** | Sequence-only reconciliation can delete non-matching local audio; PR #20 adds the bounded Phase 1 rule that compact accepted ranges may delete only local chunks from the server session's current capture epoch. |
| #13 | **open blocker** | With an expected middle sequence absent, the session can remain in `FINALIZING` with no user path to declare the missing sequence as a gap; repeated Finish attempts also amplify useless wall-clock gaps. |
| #14 | **fixed / closed by PR #17** | A stalled HTTP request could hang Stop in `FINALIZING` forever with no reachable action. PR #17 added bounded deadlines, honest recovery, and an explicit finalization escape. |
| #18 | **fixed / closed by PR #19** | Stop could reuse a pre-Stop single-flight sync snapshot and miss the final persisted fragment. PR #19 forces one fresh bounded sync over the stable stopped spool before finalization. |

On #13, the **product behavior** is proven, but the natural cause of a missing middle local fragment is not. The executed repro deliberately removed one IndexedDB fragment to establish the precondition; it is not evidence that browsers naturally evict individual IndexedDB records.

### #18 executed remediation evidence

The #18 regression was written before the fix and explicitly uses `test.describe.configure({ retries: 0 })`. It holds the first ordinary-recording chunk PUT open, presses Stop after local evidence exists, and requires Stop to converge to terminal completion after the final MediaRecorder fragment is persisted.

On the test-only branch state, GitHub Actions run `34352210865` reproduced the defect deterministically: the new test failed with the recorder message:

```text
Capture stopped, but some audio still needs a server ACK. Finish recovery later.
```

PR #19 changes the stopped-recorder boundary only. After `stopMediaRecorder()` and `chunkChain` have completed, Stop now:

1. captures any pre-existing `syncPromise`;
2. aborts that pre-Stop pass through its existing `AbortController`;
3. waits for that bounded pass to settle;
4. if finalization was not explicitly deferred, starts exactly one fresh sync pass over the now-stable stopped spool;
5. proceeds to finalization only after that pass has had the opportunity to ACK the final local high-water.

This does not weaken durable ACK semantics. A PUT that committed server-side before cancellation remains safe to reconcile or retry through the existing idempotent ingest contract, and the helper does not introduce an unbounded drain loop.

Code witness before documentation reconciliation: `56ca7ce6121683c093914ecedd4828df4abd7515`. GitHub Actions run `34352981065` on that head completed successfully across backend, frontend, Chromium e2e, and Compose smoke. E2E was clean: **18/18 passed** on the first attempt, including the retries-disabled #18 regression.

PR #19 is merged to `main` at `50eaf450d4746569160876d81beb2f97c432c288`, closing #18. Post-merge `main` CI run `34355423171` completed successfully.

### #11/#12 executed remediation evidence

PR #20 is a coordinated Phase 1 safety slice because the executed B2→B3 composition can currently destroy local audio: a stale generation keeps emitting evidence, a newer generation accepts the same sequence with different bytes, and sequence-only reconciliation can delete the older local fragment. The two issue responsibilities remain distinct.

For #11, a server-authoritative stale-writer conflict from either heartbeat or chunk upload now fences the browser generation. The browser uses the already-bounded Stop path to stop `MediaRecorder`/microphone capture, releases the capture lock, retains emitted local evidence, labels it **orphaned local evidence — not safely syncable**, and withholds Resume / Finish recovered / Sync actions that the stale ownership would make invalid. Reload also re-derives fencing from server epoch/writer truth. The existing lost-successful-claim exception is preserved. No heartbeat stall is converted into an audio gap.

For #12, compact accepted ranges may delete a local chunk only when that chunk belongs to the server session's current `capture_epoch`. This is explicitly an **interim Phase 1 safety guard**. It does not define the future per-sequence multi-client reconciliation identity contract, which remains ADR-gated before native/multi-device semantics harden.

The retries-disabled Playwright suite exercises both heartbeat-triggered and chunk-triggered fencing. Its B2→B3 composition then has a newer epoch accept the victim sequence with a different SHA, completes that newer generation, reloads the page, and proves the exact older local fragment (sequence + epoch + SHA + byte length) still survives as orphaned evidence.

Code witness before this documentation reconciliation: `8194fe65e2b2597594f973b729d378ff80403236`. GitHub Actions run `34359040563` completed successfully across backend, frontend, Chromium E2E, and Compose smoke. E2E was clean: **20/20 passed** on the first attempt; the new fenced-capture suite explicitly sets `retries: 0`.

This is **PR evidence until PR #20 merges**. Do not describe #11/#12 as fixed on `main` before then.

Consequences for current planning:

- "Browser disappearance without a clean Stop creates an interruption" remains unreliable until #10 is fixed.
- "Already-spooled evidence remains recoverable after ownership rotates" remains unsafe until #11/#12 are fixed.
- The server correctly refuses incomplete finalization, but the browser still needs an explicit missing-sequence resolution path (#13).
- The clean-Stop single-flight race is fixed on `main` by PR #19 without weakening #14 behavior.

Remaining blocker order after PR #20: **#13**, then **#10**, then the required real Chrome/Edge desktop witness. Keep those as bounded work items unless executed evidence requires otherwise.

## Phase 2 / downstream gate

Issue #15 records that a terminal `COMPLETE` session carries no classification distinguishing fully durable audio, partially gapped audio, and no audio at all. A session with zero durable fragments and zero gaps is `COMPLETE` today, and the recorder can report that every expected sequence was durably acknowledged.

This is **not a current runtime failure** because no STT, diarization, summary, or Celery consumer exists yet. It is a contract gap that must be closed before the first downstream consumer is written, and it gates the start of Phase 2 rather than the close of Phase 1.

## Remaining Phase 1 work before closing Issue #5

In order:

1. Review and merge the proven #11/#12 safety remediation in PR #20.
2. Fix #13's explicit missing-sequence resolution and gap-amplification behavior.
3. Fix #10's liveness/interruption semantics without automatically equating a heartbeat stall with audio loss.
4. Run the required real desktop Chrome/Edge background/minimized witness only after the blockers above are fixed.

For the desktop witness, use an awake desktop/laptop, start a real microphone recording, background/minimize the browser while switching among ordinary applications for a bounded interval, then return and Stop. Record the exact OS, browser/version, duration, and final continuity/ACK evidence.

CI cannot substitute for that platform/lifecycle witness.

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

PR #20 is the current coordinated-but-bounded #11/#12 safety remediation. It must remain unmerged until final review and CI on the final documentation head are complete. After #20 merges, #13 is the immediate implementation blocker.

Do **not** pull Groq, Whisper, diarization, or LLM summaries into Phase 1. The recording path must be trustworthy independently first.

## Open decisions intentionally deferred

Resolve these by evidence/ADR when their implementation phase begins:

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
- **whether a server-side liveness stall implies audio discontinuity.** Liveness interruption and proven audio discontinuity are not the same claim; locally retained audio may later prove continuity (#10);
- **how a fenced or offline client may deliver a backlog under a new capture generation.** Any per-epoch sequence-space partition or equivalent cross-device backlog scheme is a multi-device ownership contract change and needs its own ADR before native/multi-device clients (#11);
- **the multi-client reconciliation identity contract** — what per-sequence acceptance identity `/recording-state` should expose and how it interacts with range compression. The Phase 1 #12 fix is interim safety, not the final multi-client contract;
- **the terminal-state vocabulary for completeness classification**, including how zero-audio completion should be represented (#15);
- **which real-world mechanisms can leave an expected middle fragment absent from the local spool.** #13 proves the product dead-end given that precondition, not its natural cause.

## Known design boundaries

- Browser background/minimized recording on an awake desktop is a core web use case still requiring a real-platform witness.
- With the currently pinned Playwright version, automated Chromium launch disables background throttling, so the automated background-tab test cannot stand in for that witness.
- Closing the browser, sleeping/shutting down the computer, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage may be best-effort unless persistent storage is granted; UI must reflect degraded/unsafe recovery state.
- A currently open MediaRecorder fragment is not yet a durable IndexedDB/server chunk; abrupt process/device loss can lose an un-emitted bounded tail even when all earlier emitted fragments are safe.
- A server ACK is the strong durability boundary.
- WebSocket is for realtime updates, not durable state.
- Redis is not durable source of truth.
- Raw audio is not stored as database blobs.
- Multiple simultaneous sessions are a normal operating condition.
- A single live session has one active capture generation at a time; each resumed generation is fenced by fresh writer identity + incremented epoch.
- PR #20 proves a terminal fenced-client path and honest orphaned-evidence UI for #11, but that behavior is not `main` truth until the PR merges.
- PR #20 proves an interim same-epoch deletion guard for #12, but the proper multi-client reconciliation identity contract remains deliberately deferred.
- If an expected middle fragment is absent from the local spool at finalization, the product currently has no explicit terminal-resolution path (#13).
- PR #19's bounded fresh stopped-spool sync for #18 is merged on `main` at `50eaf450d4746569160876d81beb2f97c432c288`.
- Cross-device takeover is not yet claimed.
- Public upstream must remain free of deployment secrets and organization-private data.

## Local development notes

- `apps/api/tests/conftest.py` drops the entire schema on teardown. Point `DATABASE_URL` at a dedicated test database; running `pytest` against a database an API process is using destroys recording tables while leaving `alembic_version` at head, so a subsequent `alembic upgrade head` is a no-op and the API 500s.
- `apps/web/openapi-ts.config.ts` defaults to `http://localhost:8000`. On IPv6-first hosts this can fail against an API bound to `127.0.0.1`; set `OPENAPI_INPUT` explicitly.

## Handoff rule

Whenever a change materially alters current product/architecture truth, update this file in the same PR.
