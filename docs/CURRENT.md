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

Phase 2 gate Issue #15 was resolved by PR #29, squash-merged to `main` as `ba5c91145d70334335f06e8447f9310d8dac165b`. Post-merge CI run `34437634299` completed successfully across backend, frontend, Chromium E2E, and Compose smoke.

Phase 2A Issue #30 was resolved by PR #31, squash-merged to `main` as `1aba22e22b859e84dfb016bc2bd211e067467e9c`. Post-merge CI run `34439502585` completed successfully. That merge established the canonical transcript segment store and reconnect-safe HTTP read model.

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

## Canonical transcript foundation — MERGED

PR #31 / Issue #30 established the provider-neutral canonical transcript source of truth before any provider execution:

- PostgreSQL-backed immutable committed `TranscriptSegment` rows;
- stable segment ID plus owning `session_id`;
- session-local monotonically increasing transcript `sequence` used as publication/reconnect cursor;
- opaque per-session producer key for retry idempotency;
- explicit `start_ms` / `end_ms` on the recording timeline;
- canonical text and optional language;
- database uniqueness on `(session_id, sequence)` and `(session_id, producer_key)`;
- identical producer-key retries return the existing segment while conflicting retries fail explicitly;
- sequence allocation locks only the owning session row, so independent meetings do not globally block each other;
- reconnect-safe `GET /api/v1/sessions/{session_id}/transcript?after_sequence=...&limit=...` returns ordered canonical segments and a continuation cursor.

Transcript `sequence` is a publication/reconnect namespace, not the recording-chunk sequence and not an implicit timestamp. Timeline evidence remains explicit in `start_ms` / `end_ms`.

No Groq/Whisper, VAD, Celery worker, WebSocket transcript delivery, diarization, or LLM was added by this slice.

## Current Phase 2 slice — Issue #32 / PR #33

Issue #32 is the current Phase 2B task. Draft PR #33 on branch `phase2/durable-utterance-work` establishes the durable provider-neutral speech-sized audio-work contract that future VAD/repair producers create and future STT workers consume.

The candidate contract is:

- PostgreSQL-backed `TranscriptionUtterance` work rows;
- deterministic work UUID derived from `(session_id, canonical producer_key)` so a retry after a process crash addresses the same storage object;
- session-local monotonically increasing utterance `sequence` with sequence allocation serialized by locking only the owning session row;
- explicit `start_ms` / `end_ms` on the recording timeline;
- normalized audio `content_type`, SHA-256, byte length, and durable storage key;
- independently decodable utterance media stored through the owned filesystem audio-storage boundary before the database row is acknowledged;
- stable storage path `sessions/<session-id>/utterances/<work-uuid>.media`, so a matching object left by a crash before DB commit is safely reusable on retry;
- identical producer-key retries are idempotent; timing/content-type/byte conflicts fail explicitly;
- inspection read `GET /api/v1/sessions/{session_id}/utterances?after_sequence=...&limit=...`; the HTTP response does not expose the filesystem storage key;
- deterministic future canonical transcript producer key `utterance:<work-uuid>` so provider retries cannot create duplicate transcript rows for one work item.

Archive `MediaRecorder` fragments and transcription utterance work remain different things. Archive fragments are durability evidence and are not assumed independently decodable or semantically aligned to speech. The utterance producer is responsible for creating an independently decodable speech-sized object; this contract does not reconstruct arbitrary raw-fragment subsets into standalone media.

The recording ingest/ACK path does not call the utterance path. Capture remains safe even if utterance creation or all downstream intelligence is unavailable.

PR #33 deliberately does **not** implement Groq/Whisper/faster-whisper calls, a VAD algorithm, Celery execution, realtime PCM/WebSocket ingress, diarization, or LLM summaries.

## Exact next dependency after Issue #32

After #32 is merged and post-merge evidence is green, the next bounded Phase 2 slice should build the **realtime utterance producer**:

- capture a realtime PCM/Web Audio lane independently from the archive lane;
- run VAD/endpoint detection to identify speech-sized intervals;
- encode each committed interval as independently decodable media and commit it through the durable utterance-work contract;
- preserve recording-timeline evidence and deterministic producer identity;
- benchmark and tune minimum speech, silence endpoint, and hard maximum boundaries against real office audio;
- prove realtime-lane degradation cannot weaken archive recording or durable chunk ACKs.

Only after that producer boundary is proven should the Groq `STTProvider` and live queue consume committed utterance work and write canonical transcript segments using `utterance:<work-uuid>` producer keys.

## Production exposure boundary

Phase 1.5 authentication, authorization, retention, and deletion work remains required before Live Intelligence is exposed as a production service. Trusted local development of Phase 2 may continue before that deployment boundary is finished.

## Not implemented yet

- Groq or local STT provider execution;
- realtime PCM/Web Audio utterance producer;
- VAD/endpointing implementation and tuning;
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
- Transcript revision/supersession semantics remain deferred; a producer-key retry may not silently change committed evidence.
- Speaker/diarization semantics are intentionally absent from the first canonical transcript row.
- The durable utterance contract does not choose the VAD algorithm, realtime transport framing, utterance codec, or provider model; those require the next implementation/benchmark slice.
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
5. read Issue #32 and PR #33 (or their successors) plus current CI evidence;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
