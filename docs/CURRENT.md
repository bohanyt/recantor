# Current

Last updated: 2026-09-10

This file is the short operational source of truth for Recantor. Inspect GitHub fresh before acting; repository, PR, issue, and CI state outrank this summary if the repository has moved.

## Product truth

Recantor is a public, self-hosted recording and meeting-intelligence project with two intended web workflows:

1. **Live Intelligence** — authenticated live recording, transcript, speaker processing, and rolling meeting intelligence.
2. **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then process/export it. A bounded guest path may operate without login.

The implemented foundation is reliable capture first. Desktop Chrome/Edge on an awake computer is the first web reliability target. Mobile web remains usable while active, but Recantor does not promise continuous browser recording through screen lock, OS suspension, sleep, or shutdown.

> Capture is infrastructure. Intelligence is downstream.

Phase 1 capture is closed. Phase 2 transcript work may now proceed, but capture must remain independent from STT, diarization, realtime delivery, and LLM availability.

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
- Redis for ephemeral coordination/background-job infrastructure;
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

Phase 1 closed after the real-platform witness. The witness ran against `9a1d27bfaf19d14cacd17d89d8c9f8a7eaf2c1d7`; closure docs landed as `cd9c35543be8881acd682b14b031b0786a81a1a7` and CI `34430899348` succeeded.

Phase 2 gate Issue #15 was then resolved by PR #29, squash-merged to `main` as `ba5c91145d70334335f06e8447f9310d8dac165b`. Post-merge CI run `34437634299` completed successfully across backend, frontend, Chromium E2E, and Compose smoke.

This document may itself live in a later branch/commit; use GitHub `main` as the authoritative integration head rather than treating a checkpoint above as a self-referential branch SHA.

## Phase 1 status — CLOSED

All validated Phase 1 code blockers are merged/closed: #10, #11, #12, #13, #14, and #18. Issue #5 is closed.

The final Case K witness established the bounded claim for an awake Windows desktop with an ordinary Edge browser and actual microphone:

- checkout `9a1d27bfaf19d14cacd17d89d8c9f8a7eaf2c1d7`;
- local Docker Compose stack, browser at `http://localhost:5173`;
- built-in laptop microphone;
- Microsoft Edge `152.0.4191.66`, Chromium `152.0.7977.83`;
- Windows 11 25H2 build `26200.9168`;
- visible checkpoint at `00:01:05`: ACK through sequence `31`, one pending local fragment, gaps `0`;
- entire Edge window minimized for about five minutes while the laptop remained awake;
- final checkpoint at `00:06:05`: `COMPLETE`, Server synced, ACK through sequence `179`, pending local audio `0`, gaps `0`;
- PostgreSQL witness session had `final_sequence=179`, 179 unique chunks spanning sequence 1 through 179, and zero gaps.

Do not generalize this to desktop sleep/shutdown, execution-suspending lock behavior, or mobile browser suspension.

## Terminal audio completeness — MERGED

PR #29 / Issue #15 separates lifecycle completion from audio completeness. `state == complete` means the declared final sequence boundary is fully accounted for. It does **not** by itself mean gap-free or non-empty audio.

`audio_completeness` is persisted in the finalize transaction and exposed on the session resource:

- `full` — non-zero final boundary and every expected sequence is durably present;
- `partial` — non-zero final boundary and at least one expected sequence is represented by an explicit sequence-loss gap, including an all-gap result;
- `empty` — final sequence boundary is zero and no audio sequence was captured.

Wall-clock-only liveness/interruption evidence does not by itself downgrade `full`. A retry of an already-`COMPLETE` finalize preserves the persisted classification. `FinalizeSessionResponse.complete` remains a compatibility signal meaning only that the declared boundary is accounted for.

Downstream consumers must use `audio_completeness` rather than treating lifecycle `COMPLETE` as proof that audio exists or is gap-free.

## Current Phase 2 slice — Issue #30 / PR #31

Issue #30 is the current bounded Phase 2A task: establish the provider-neutral canonical transcript source of truth and reconnect-safe HTTP read model **before** Groq, VAD, Celery queue wiring, or WebSocket transcript delivery.

PR #31 on branch `phase2/canonical-transcript-foundation` is the implementation candidate. Its intended contract is:

- PostgreSQL-backed immutable committed `TranscriptSegment` rows;
- stable segment ID plus owning `session_id`;
- session-local monotonically increasing transcript `sequence` used as publication/reconnect cursor;
- opaque per-session producer key for retry idempotency;
- explicit `start_ms` / `end_ms` on the recording timeline;
- canonical text and optional language;
- database uniqueness on `(session_id, sequence)` and `(session_id, producer_key)`;
- internal retry-safe commit function: identical producer-key retries return the existing segment, conflicting retries fail explicitly;
- sequence allocation serialized by locking only the owning session row so independent meetings do not globally block each other;
- reconnect-safe `GET /api/v1/sessions/{session_id}/transcript?after_sequence=...&limit=...` returning ordered canonical segments and a continuation cursor.

Transcript `sequence` is a publication/reconnect namespace, not the recording-chunk sequence and not an implicit timestamp. Timeline ordering evidence remains explicit in `start_ms` / `end_ms`.

This slice deliberately does **not** call Groq/Whisper, run VAD, create Celery workers, push WebSockets, perform diarization, or invoke an LLM. Those remain downstream of a stable canonical transcript contract.

## Exact next dependency after Issue #30

After #30 is merged and post-merge CI is green, the next bounded Phase 2 design/implementation slice should establish the **utterance/audio-work contract** that feeds STT:

- define how speech-sized work is identified from live audio without weakening the archive lane;
- retain timeline/source evidence needed to associate provider output with the recording;
- define retry/idempotency identity from utterance work into canonical transcript producer keys;
- benchmark/tune VAD/endpoint boundaries against real office audio;
- only then wire the Groq `STTProvider` and live queue against that contract.

Do not send arbitrary standalone MediaRecorder fragments directly to STT merely because they are convenient; archive fragments are not assumed independently decodable and their timeslice is not a semantic utterance boundary.

## Production exposure boundary

Phase 1.5 authentication, authorization, retention, and deletion work remains required before Live Intelligence is exposed as a production service. Trusted local development of Phase 2 may continue before that deployment boundary is finished.

## Not implemented yet

- Groq or local STT provider execution;
- utterance/VAD audio-work pipeline;
- live STT Celery queue;
- realtime transcript WebSocket delivery;
- transcript UI/reconnect client;
- diarization;
- rolling/final summaries;
- production authentication/authorization;
- user-visible recording deletion/retention;
- tus existing-recording upload pipeline;
- production reverse-proxy/deployment hardening;
- native Android/iOS recorder clients.

Do not describe these as working until repository evidence proves them.

## Known boundaries and deferred decisions

- Closing the browser, desktop sleep/shutdown, or mobile OS suspension cannot be treated as continuous capture.
- Browser local storage remains best-effort unless persistence is granted; the UI must expose degraded/unsafe recovery state.
- An open MediaRecorder fragment is not yet durable browser/server evidence; abrupt process/device loss can lose an un-emitted bounded tail.
- Fenced old-generation evidence is retained locally but has no final operator recovery/export/expiry flow yet.
- #12's same-epoch reconciliation guard is an interim safety measure, not a final multi-device identity contract.
- Cross-device takeover/backlog semantics are not claimed.
- Transcript revision/supersession semantics are not part of #30; a producer-key retry may not silently change committed evidence.
- Speaker/diarization semantics are intentionally absent from the first canonical transcript row.
- Public upstream must remain free of organization secrets, private data, and downstream-only branding/infrastructure.

## Local development

The simplest runnable stack requires Docker with Docker Compose:

```bash
git clone https://github.com/bohanyt/recantor.git
cd recantor
docker compose -f infra/compose.yaml up --build
```

Open exactly `http://localhost:5173`. The API is at `http://localhost:8000`.

`apps/api/tests/conftest.py` drops the entire test schema on teardown; never point pytest at a database used by a running API instance.

## Handoff rule

For a new Control Tower chat:

1. inspect GitHub fresh;
2. read `AGENTS.md`;
3. read this file;
4. read the latest dated file under `docs/handoff/` if present;
5. read Issue #30 and PR #31 (or their successors) plus current CI evidence;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
