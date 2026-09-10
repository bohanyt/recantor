# Current

Last updated: 2026-09-10

This file is the short operational source of truth for Recantor. Inspect GitHub fresh before acting; repository, PR, issue, and CI state outrank this summary if the repository has moved.

## Product truth

Recantor is a public, self-hosted recording and meeting-intelligence project with two intended web workflows:

1. **Live Intelligence** — authenticated live recording, transcript, speaker processing, and rolling meeting intelligence.
2. **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then process/export it. A bounded guest path may operate without login.

The implemented foundation is reliable capture first. Desktop Chrome/Edge on an awake computer is the first web reliability target. Mobile web remains usable while active, but Recantor does not promise continuous browser recording through screen lock, OS suspension, sleep, or shutdown.

> Capture is infrastructure. Intelligence is downstream.

STT, diarization, transcript intelligence, and LLM summaries remain downstream and were intentionally absent from Phase 1.

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

PR #28 was squash-merged on 2026-09-10 and closed Issue #10. Post-merge implementation CI run `34419675987` completed successfully across backend, frontend, Chromium E2E, and Compose smoke.

The real-platform witness was executed against `main` commit `9a1d27bfaf19d14cacd17d89d8c9f8a7eaf2c1d7`. Its docs-only CI run `34419972995` also completed successfully. Phase 1 closure docs are on `main` at `cd9c35543be8881acd682b14b031b0786a81a1a7`, whose CI run `34430899348` completed successfully.

This document may itself live in a later branch/commit; use GitHub `main` as the authoritative production-integration head rather than treating a checkpoint above as a self-referential branch SHA.

### Merged Phase 1 deliveries

- **PR #6** — server durability core: lifecycle, writer/epoch fencing, sequenced ingest, crash-safe filesystem commit, PostgreSQL acceptance metadata, accepted-range reconciliation, explicit gaps, final high-water enforcement.
- **PR #7** — browser recovery recorder: MediaRecorder, Dexie/IndexedDB spool, atomic local sequence/timing high-water persistence, SHA-256 evidence, bounded retry/backoff, reconnect/manual sync, storage-safety reporting, recovery UX, final fragment flush.
- **PR #9** — capture-generation recovery/fencing with recovery capability, fresh writer/epoch on resume, stale-writer rejection, and persisted pending claim intent.
- **PR #17 / Issue #14** — bounded recorder requests and escapable finalization.
- **PR #19 / Issue #18** — deterministic clean Stop sync over the stable stopped spool.
- **PR #20 / Issues #11 and #12** — terminal stale-writer browser fencing plus bounded same-epoch reconciliation safety.
- **PR #27 / Issue #13** — explicit missing-sequence resolution, non-destructive retry, explicit permanent-loss declaration, late-fill completion, and stable gap accounting.
- **PR #28 / Issue #10** — heartbeat checks stale liveness before refreshing `last_heartbeat_at`; `state == interrupted` means a current liveness hole while `interrupted_at` retains the latest historically observed interruption; heartbeat/claim recovery returns to `recording` without fabricating an audio gap.

## Phase 1 status — CLOSED

All validated code blockers discovered by the executed Phase 1 re-review are merged and closed: #10, #11, #12, #13, #14, and #18.

Issue #5's final remaining acceptance item, Case K (real desktop background/minimized behavior), passed on 2026-09-10 with the following bounded witness:

- checkout under test: `9a1d27bfaf19d14cacd17d89d8c9f8a7eaf2c1d7`;
- full local Docker Compose stack on the same laptop; web origin `http://localhost:5173`;
- actual built-in laptop microphone, with audible microphone capture confirmed in an immediately preceding local smoke recording;
- Microsoft Edge `152.0.4191.66` (64-bit), Chromium `152.0.7977.83`;
- Edge reported Windows 11 Version 25H2, build `26200.9168`; PowerShell reported Microsoft Windows 11 Pro for Workstations, version `10.0.26200`, build number `26200`;
- ordinary Edge Default profile; `edge://version` showed no special headless/background-throttling-disabling flags;
- recording visible checkpoint at `00:01:05`: server ACK through sequence `31`, one pending local fragment, network online, explicit gaps `0`;
- entire Edge window then minimized for about five minutes while the laptop remained awake;
- restored/finalized checkpoint at `00:06:05`: state `COMPLETE`, durability `Server synced`, server ACK through sequence `179`, pending local audio `0`, network online, explicit gaps `0`;
- PostgreSQL session `a776e0d5-4e5b-4ef7-b087-16bdd6328e04`: state `complete`, `final_sequence=179`, `chunks=179`, `min_seq=1`, `max_seq=179`, `gaps=0`;
- `recording_chunks` has a unique `(session_id, sequence)` constraint, so 179 accepted rows spanning sequence 1 through 179 establishes a contiguous accepted ledger for this witness.

Result: Case K is satisfied for this exact awake Windows 11 + Edge configuration and approximately five-minute minimized interval. Do **not** generalize this result to desktop sleep/shutdown, execution-suspending lock behavior, or mobile browser suspension.

Issue #5 is closed as the Phase 1 umbrella. Phase 1 capture reliability is complete at its stated boundary.

## Liveness semantics after PR #28

- `session.state == interrupted` means the session is **currently** considered inside a liveness hole.
- `interrupted_at` is historical evidence: the latest heartbeat timestamp at which an observed timeout began. It may remain populated after the session returns to `recording` or later completes.
- a valid heartbeat evaluates the previous heartbeat timestamp before replacing it, so a timeout is recorded even when no GET/read occurred during the stall;
- capture claim and heartbeat recovery preserve the same historical evidence semantics;
- no heartbeat/liveness stall automatically creates a `RecordingGap`; locally retained audio can still later prove continuity.

## Current gate — Issue #15 / draft PR #29

Issue #15 is the first Phase 2 product/architecture gate and must be resolved **before the first downstream STT/summary consumer is implemented**. Draft PR #29 on branch `phase2/terminal-audio-completeness` is the current bounded implementation candidate; it is not merged yet.

The candidate separates lifecycle completion from audio completeness. `state == complete` continues to mean that the declared final sequence boundary is fully accounted for, while the session persists `audio_completeness` at the same finalize transaction:

- `full` — the declared boundary is non-zero and every expected sequence is durably present;
- `partial` — the declared boundary is non-zero and at least one expected sequence is satisfied by an explicit sequence-loss gap, including an all-gap result;
- `empty` — the declared sequence boundary is zero and no audio sequence was captured.

Wall-clock-only interruption/liveness evidence does not by itself downgrade `full`; #10 established that liveness interruption is not equivalent to proven audio loss. A retry of an already-`COMPLETE` finalize preserves the persisted classification instead of recomputing it.

The candidate retains `FinalizeSessionResponse.complete` for compatibility, but its meaning is only **final boundary fully accounted for**. Downstream consumers must use `audio_completeness` rather than interpreting `complete == true` or lifecycle `COMPLETE` as proof of gap-free audio.

The candidate also prevents a zero-audio browser completion from displaying the misleading message that every expected audio sequence was durably acknowledged.

No Groq/Whisper/faster-whisper STT, VAD, diarization, rolling/final LLM summary, or other downstream intelligence consumer has been started in this gate.

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
4. read the latest dated file under `docs/handoff/` if present;
5. read Issue #15 and PR #29 (or their successors) plus current CI evidence;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
