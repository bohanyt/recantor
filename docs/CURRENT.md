# Current

Last updated: 2026-09-10

This file is the short operational source of truth for Recantor. Inspect GitHub fresh before acting; repository, PR, issue, and CI state outrank this summary if the repository has moved.

## Product truth

Recantor is a public, self-hosted recording and meeting-intelligence project with two intended web workflows:

1. **Live Intelligence** — authenticated live recording, transcript, speaker processing, and rolling meeting intelligence.
2. **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then process/export it. A bounded guest path may operate without login.

The currently implemented production-critical work is the capture foundation. Desktop Chrome/Edge on an awake computer is the first web reliability target. Mobile web remains usable while active, but Recantor does not promise continuous browser recording through screen lock, OS suspension, sleep, or shutdown.

> Capture is infrastructure. Intelligence is downstream.

STT, diarization, transcript intelligence, and LLM summaries remain downstream and are intentionally absent from Phase 1.

## Implemented capture architecture

The browser/server durability path is:

```text
MediaRecorder
  -> atomic Dexie/IndexedDB fragment + local high-water commit
  -> sequenced HTTP upload with hash/timing/ownership evidence
  -> crash-safe filesystem audio commit + PostgreSQL acceptance metadata
  -> durable HTTP ACK
  -> local fragment deletion
```

Selected foundation:

- React + TypeScript + Vite + Tailwind CSS web SPA;
- TanStack Query for server state;
- Python + FastAPI + Pydantic API;
- PostgreSQL + SQLAlchemy 2 + Alembic durable structured state;
- Redis available for ephemeral coordination/background-job infrastructure;
- Dexie/IndexedDB browser recovery spool;
- Docker Compose development/deployment baseline;
- Node.js 24 LTS and Python 3.13.

Important capture guardrails:

- IndexedDB is a recovery spool, not the final durability boundary;
- a server ACK requires durable audio plus durable acceptance metadata;
- one live session has one active capture generation at a time, fenced by writer identity + epoch;
- already-spooled evidence must not be silently destroyed when ownership rotates;
- raw MediaRecorder chunks are ordered media fragments and are not assumed independently decodable;
- Stop/finalize declares a final sequence/high-water boundary;
- every expected sequence through that boundary must be durably present or explicitly represented as loss before completion;
- liveness interruption is not automatically audio loss;
- PostgreSQL/audio storage are durable truth; Redis/WebSocket are not.

## Repository checkpoint

Latest merged implementation checkpoint: `47a18f24f8a2eaa621669392c52185b816e1fc67` — PR #28, **liveness interruption semantics**.

PR #28 was deliberately squash-merged on 2026-09-10 and closed Issue #10. Post-merge `main` CI run `34419675987` completed successfully across backend, frontend, Chromium E2E, and Compose smoke.

This document may itself live in a later docs-only commit; use GitHub `main` as the authoritative head rather than treating the implementation checkpoint above as a self-referential branch SHA.

### Merged Phase 1 deliveries

- **PR #6** — server durability core: lifecycle, writer/epoch fencing, sequenced ingest, crash-safe filesystem commit, PostgreSQL acceptance metadata, accepted-range reconciliation, explicit gaps, final high-water enforcement.
- **PR #7** — browser recovery recorder: MediaRecorder, Dexie/IndexedDB spool, atomic local sequence/timing high-water persistence, SHA-256 evidence, bounded retry/backoff, reconnect/manual sync, storage-safety reporting, recovery UX, final fragment flush.
- **PR #9** — capture-generation recovery/fencing with recovery capability, fresh writer/epoch on resume, stale-writer rejection, and persisted pending claim intent.
- **PR #17 / Issue #14** — bounded recorder requests and escapable finalization.
- **PR #19 / Issue #18** — deterministic clean Stop sync over the stable stopped spool.
- **PR #20 / Issues #11 and #12** — terminal stale-writer browser fencing plus bounded same-epoch reconciliation safety.
- **PR #27 / Issue #13** — explicit missing-sequence resolution, non-destructive retry, explicit permanent-loss declaration, late-fill completion, and stable gap accounting.
- **PR #28 / Issue #10** — heartbeat checks stale liveness before refreshing `last_heartbeat_at`; `state == interrupted` means a current liveness hole while `interrupted_at` retains the latest historically observed interruption; heartbeat/claim recovery returns to `recording` without fabricating an audio gap.

## Phase 1 status

All validated code blockers discovered by the executed Phase 1 re-review are now merged and closed: #10, #11, #12, #13, #14, and #18.

Issue #5 remains open because one exit requirement is intentionally not automatable with the current CI environment:

### Real desktop Chrome/Edge witness — current next step

Run Recantor on an **awake real Windows/desktop machine** with an actual microphone and an ordinary Chrome or Edge build. Do not use Playwright/headless flags as evidence.

Preferred witness setup is the entire Docker Compose development stack on the same laptop and the browser pointed at `http://localhost:5173`. This isolates the real-browser lifecycle behavior from unrelated deployment/network variables while still exercising the real web -> API -> PostgreSQL/audio-storage path.

Suggested bounded witness:

1. record for about 1 minute with the browser visible;
2. note elapsed time and the current server-ACK sequence;
3. minimize the entire browser window for about 5 minutes while keeping the computer awake and unlocked enough that the OS does not suspend it;
4. restore the window and confirm elapsed time/capture advanced;
5. allow any pending local audio to synchronize;
6. Stop normally;
7. record exact Git commit, Windows version/build, browser/version, minimized interval, terminal session state, final sequence/accepted continuity, pending-local count, and explicit gaps.

Passing evidence should show capture continued during the tested minimized interval and the resulting recording reconciled honestly. Do not generalize the result to laptop sleep/shutdown or mobile browser suspension.

After a successful real-platform witness, reconcile Issue #5 against its acceptance matrix and close Phase 1 if no new defect is revealed.

## Liveness semantics after PR #28

- `session.state == interrupted` means the session is **currently** considered inside a liveness hole.
- `interrupted_at` is historical evidence: the latest heartbeat timestamp at which an observed timeout began. It may remain populated after the session returns to `recording` or later completes.
- a valid heartbeat evaluates the previous heartbeat timestamp before replacing it, so a timeout is recorded even when no GET/read occurred during the stall;
- capture claim and heartbeat recovery preserve the same historical evidence semantics;
- no heartbeat/liveness stall automatically creates a `RecordingGap`; locally retained audio can still later prove continuity.

## Phase 2 gate

Issue #15 is **not a Phase 1 blocker**, but it must be resolved before the first downstream STT/summary consumer is implemented.

Today lifecycle `COMPLETE` alone does not distinguish fully durable audio, partial/gapped audio, and zero-audio completion. #15 will establish a persisted terminal completeness/continuity classification so downstream intelligence cannot infer more than capture evidence proves.

## Not implemented yet

- Groq or local STT;
- realtime transcript delivery;
- diarization;
- rolling/final summaries;
- production Celery processing consumers;
- tus existing-recording upload pipeline;
- production authentication/authorization;
- recording deletion/retention UI;
- production reverse-proxy/deployment hardening;
- native Android/iOS recorder clients.

Do not describe those as working until repository evidence proves them.

## Known boundaries and deferred decisions

- Closing the browser, desktop sleep/shutdown, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage remains best-effort unless persistence is granted; the UI must expose degraded/unsafe recovery state.
- An open MediaRecorder fragment is not yet durable browser/server evidence; abrupt process/device loss can lose an un-emitted bounded tail.
- Fenced old-generation evidence is retained locally but has no final operator recovery/export/expiry flow yet.
- #12's same-epoch reconciliation guard is an interim Phase 1 safety measure, not a final multi-device identity contract.
- Cross-device takeover/backlog semantics are not claimed.
- Whether `claim_capture` should ever accept a non-complete `FINALIZING` session remains deferred.
- Public upstream must remain free of organization secrets, private data, and downstream-only branding/infrastructure.

## Local development

The simplest runnable stack requires Docker with Docker Compose:

```bash
git clone https://github.com/bohanyt/recantor.git
cd recantor
docker compose -f infra/compose.yaml up --build
```

Open exactly `http://localhost:5173` for the web UI. The API is exposed at `http://localhost:8000`.

`apps/api/tests/conftest.py` drops the entire test schema on teardown; never point pytest at a database used by a running API instance.

## Handoff rule

For a new Control Tower chat:

1. inspect GitHub fresh;
2. read `AGENTS.md`;
3. read this file;
4. read the latest dated file under `docs/handoff/`;
5. read Issue #5 and the current next blocker/witness evidence;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
