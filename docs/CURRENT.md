# Current

Last updated: 2026-09-12

This file is the short operational source of truth for Recantor. Fresh GitHub state outranks this summary if a branch, PR, issue, or CI run has moved.

## Active integration line

Canonical cloud-alpha integration branch:

`integration/cloud-alpha-2026-09-11`

Issue #43 was dispatched from exact integration SHA:

`ff7d8ac46d726f0b8b6646bab33aa4d8c9412e4b`

That baseline is the Control Tower accepted integration of the #44 resumable upload foundation after #42. At dispatch, ordinary CI run `34659603660` was green across backend, frontend, Chromium E2E, and Compose smoke, and upload-foundation CI run `34659603680` was green.

The #43 implementation branch `agent-m/issue-43-product-shell` is a presentation/productization candidate on top of that baseline. It must not be treated as integrated until Control Tower accepts and integrates its DRAFT PR.

## Product truth on the integration baseline

Recantor currently has two bounded web workflows:

1. **Live** — reliable browser archive recording, an independent realtime speech lane, durable live STT scheduling, canonical transcript recovery, and the integrated #42 live transcript surface.
2. **Upload recording** — the integrated #44 Uppy+tus/tusd resumable upload foundation with durable completion evidence. This stops at durable uploaded-source evidence; uploaded-media processing is not implemented on the integration baseline.

Desktop Chrome/Edge on an **awake** computer is the first browser recording reliability target. Recantor does not claim continuous browser capture through desktop sleep/shutdown, execution-suspending lock behavior, or mobile background suspension.

> Capture is infrastructure. Intelligence is downstream.

Archive recording safety is independent from STT, transcript delivery, diarization, and LLM availability.

## Durable archive recording — merged foundation

The archive path remains:

```text
MediaRecorder
  -> atomic Dexie/IndexedDB fragment + local high-water commit
  -> sequenced HTTP upload with hash/timing/ownership evidence
  -> crash-safe filesystem audio commit + PostgreSQL acceptance metadata
  -> durable HTTP ACK
  -> local fragment deletion
```

Required invariants:

- IndexedDB is a recovery spool, not final durability;
- one live session has one active capture generation fenced by writer identity + epoch;
- Stop/finalize declares an explicit final sequence boundary;
- every expected sequence through that boundary is durable or explicitly represented as loss before completion;
- liveness interruption is not automatically audio loss;
- raw MediaRecorder fragments are ordered source media, not assumed independently decodable;
- PostgreSQL/audio storage are durable truth; Redis and WebSocket delivery are not.

Phase 1 reliable archive capture and terminal `audio_completeness` (`full` / `partial` / `empty`) are merged foundations. The bounded real Edge/Windows witness remains evidence for an awake desktop only, not for sleep/shutdown/mobile suspension.

## Canonical transcript and realtime utterance foundation — merged

Canonical transcript truth is immutable PostgreSQL `TranscriptSegment` evidence with stable session/segment identity, per-session transcript sequence, producer identity, explicit timing, text, and optional language. HTTP cursor reads recover canonical state after reconnect.

The realtime audio lane reuses the microphone stream independently from archive capture:

```text
same microphone MediaStream
        |
        +---------------- archive lane ----------------+
        |  MediaRecorder -> IndexedDB -> HTTP -> durable ACK
        |
        +--------------- realtime lane ----------------+
           AudioWorklet -> sample-clock PCM
                         -> bounded WebSocket transport
                         -> server VAD / endpointing
                         -> durable TranscriptionUtterance WAV
```

Realtime failure is downstream degradation. It never participates in archive ACK/finalization semantics.

The Groq STT provider boundary consumes committed durable utterance work, verifies storage evidence before provider execution, and commits canonical transcript evidence with deterministic producer identity. Blank/malformed/provider failure does not fabricate transcript rows and does not weaken archive capture.

## #38 durable live STT scheduler — integrated on cloud-alpha

Issue #38 is no longer merely a draft dependency on this integration line. The PostgreSQL-authoritative live STT scheduler is present in `integration/cloud-alpha-2026-09-11`.

Current scheduler properties include:

- durable PostgreSQL `STTJob` identity/state;
- claim leases and fencing for worker death/duplicate delivery;
- PostgreSQL-backed global/per-session admission and fairness;
- Celery/Redis used for wake/delivery coordination rather than durable backlog truth;
- bounded retry handling using provider error categories;
- reconciliation that can rediscover unfinished durable utterance work;
- canonical transcript evidence remains authoritative for success.

Ordinary CI does not require a real Groq secret or provider network call.

## #42 canonical live transcript UI — integrated

Issue #42 is integrated on the cloud-alpha line.

The web transcript surface preserves these semantics:

- canonical HTTP cursor reads are truth;
- WebSocket delivery is an ephemeral wake hint, not persistence;
- duplicate/out-of-order wake hints converge through canonical reads;
- reconnect/degraded delivery can catch up without changing archive recording safety;
- transcript presentation does not own recorder lifecycle or archive durability.

Issue #43 may reposition/mount this panel but does **not** change files under `apps/web/src/transcript/**` or its truth/reconnect logic.

## #44 resumable existing-recording upload foundation — integrated

Issue #44 is integrated on the cloud-alpha line at dispatch baseline `ff7d8ac46d726f0b8b6646bab33aa4d8c9412e4b`.

Current upload foundation:

```text
Browser file
  -> Uppy + tus
  -> tusd
  -> shared durable audio storage
  -> Recantor tusd completion hook
  -> PostgreSQL upload completion evidence
```

Current boundary:

- WAV/MP3/M4A/OGG/WebM/MP4 source admission;
- pause/retry/reload/reselection resumes the same tus upload when recovery evidence matches;
- capability-token protected upload-session reads/hooks;
- durable completion records internal storage identity, exact byte length, and streaming SHA-256;
- UI may claim **durably uploaded** only after Recantor confirms durable completion.

It does **not** yet mean the uploaded source has been normalized, segmented, transcribed, or exported.

## #43 product shell candidate — this branch only

The #43 candidate is limited to product presentation and setup truth:

- obvious `Live` / `Upload recording` top-level workflow shell;
- Live prioritizes lifecycle + elapsed time, one primary action, audio safety, transcription state, recovery/loss controls, then canonical transcript;
- archive-audio and transcription state are visually/semantically separate;
- fenced ownership, missing-audio loss, spool failure, recovery, and sync controls remain reachable outside Diagnostics;
- `Advanced / Diagnostics` is collapsed by default and read-only;
- #42 transcript logic remains untouched;
- #44 upload logic remains the underlying upload foundation;
- normal setup truth is centered on server-side `GROQ_API_KEY`;
- stale provider/fallback selectors are removed from `.env.example` because backend `Settings` does not implement them;
- laptop layout/accessibility/focus behavior receives dedicated web tests.

No #45 backend/media implementation is part of #43.

## Configuration truth

Backend `Settings` currently implements direct Groq configuration:

- `GROQ_API_KEY`;
- `GROQ_STT_ENDPOINT`;
- `GROQ_STT_MODEL`;
- `GROQ_STT_TIMEOUT_SECONDS`.

`STT_PRIMARY_PROVIDER`, `STT_FALLBACK_PROVIDER`, and `LOCAL_STT_BASE_URL` are **not** implemented settings and must not appear as normal setup selectors.

Docker Compose forwards `GROQ_API_KEY` to `api` and `stt-worker`. The web service never needs the Groq secret. `.env.example` is the repository template; real credentials remain local and uncommitted.

## Not implemented yet

The following must not be described as working current capabilities:

- **#45 uploaded-media processing**: FFmpeg/ffprobe validation, normalization, segmentation/timeline mapping, and durable upload-to-STT/canonical-transcript processing;
- **#46 upload result/export UX**: uploaded recording transcript/result views and TXT/JSON/VTT/SRT exports;
- local faster-whisper fallback/provider selection;
- diarization/speaker labels;
- rolling/final summaries;
- production authentication/authorization;
- user-visible retention/deletion policy UX;
- production reverse-proxy/deployment hardening;
- native Android/iOS recorder clients.

A concurrent #45 implementation lane may exist, but until its accepted work is integrated, this file and the #43 UI must not claim those processing/results capabilities.

## Production exposure boundary

The current alpha remains trusted-development software. Authentication, authorization, retention/deletion behavior, abuse controls, and production deployment hardening are required before exposing sensitive recordings/transcripts to untrusted users.

## Immediate coordination rule

- #43 owns only its bounded frontend presentation/setup/docs surfaces.
- #45 owns uploaded-media processing/backend work and must not require #43 to edit backend Python or `infra/compose.yaml`.
- #46 remains downstream of #45 for uploaded-recording result/export behavior.
- No local Windows acceptance is part of #43; ordinary CI/cloud Chromium evidence is the gate before handoff.
