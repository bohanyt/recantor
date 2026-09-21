# Current

Last updated: 2026-09-21

This file is the short operational source of truth for Recantor. Fresh GitHub state outranks this summary if a branch, PR, issue, or CI run has moved.

## Active integration line

Canonical cloud-alpha integration branch:

`integration/cloud-alpha-2026-09-11`

The integration line now contains the independently reviewed candidates for:

- #42 realtime transcript delivery + Live transcript UI;
- #44 resumable existing-recording upload foundation;
- #45 uploaded-media normalization + durable upload-to-transcript processing, accepted exact head `18453eaabea11fac01f664f73c949c7b2ea7f32c`;
- #43 product shell / truthful setup, accepted exact head `0dc2295e496da76ff8a9921140ed8be6e95877d5`;
- #46 Upload processing/result UI + TXT/JSON/VTT/SRT exports, independently CLEAN at exact head `005c49c4b32fb3c0fd2abe5c3ee9abe5316b48e0` and fast-forward integrated to the canonical line.

The #45 candidate was integrated first because it establishes backend/media/runtime truth. The #43 product/docs candidate followed. The independently accepted #46 candidate was then fast-forward integrated, and fresh exact integrated-head CI / Upload foundation CI / Media processing CI were green before the #48 cloud gate opened.

No merge to `main` is authorized by this integration work. PR #53 remains the cloud-alpha checkpoint vehicle.

## Product truth on the integration line

Recantor currently has two bounded web workflows:

1. **Live** — reliable browser archive recording, an independent realtime speech lane, PostgreSQL-authoritative live STT scheduling, canonical transcript recovery, and the #42 reconnect-safe Live transcript surface.
2. **Upload recording** — Uppy+tus/tusd resumable transfer with durable completion evidence, followed by PostgreSQL-authoritative media processing, bounded ffprobe/FFmpeg normalization, deterministic D3-A segmentation, upload-class STT scheduling, and canonical `TranscriptSegment` production.

The Upload backend and product result path now reach canonical transcript truth. The capability-protected Upload UI exposes processing/result state, canonical recording-timeline transcript presentation, terminal recovery semantics, and TXT/JSON/VTT/SRT exports derived from canonical `TranscriptSegment` rows.

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

The Groq STT provider boundary consumes committed durable utterance work, verifies storage evidence before provider execution, and commits canonical transcript evidence with deterministic producer identity.

## Durable STT scheduling — integrated

PostgreSQL owns durable `STTJob` identity/state, retry budget, delivery reservations, claims, and canonical convergence. Redis/Celery carry generic wake signals only.

Current scheduler properties include:

- claim leases and fencing for worker death/duplicate delivery;
- PostgreSQL-backed admission and per-session fairness;
- bounded retry handling using provider error categories;
- reconciliation that rediscovers unfinished durable utterance work;
- canonical transcript evidence remains authoritative for success;
- terminal `no_speech` for valid blank provider results, without creating blank transcript rows;
- separate workload classes and worker/queue capacity for Live (`stt-live`) and Upload (`stt-upload`).

Upload backlog therefore does not consume the reserved Live STT frontier. Ordinary CI does not require a real Groq secret or provider network call; focused proof exercises the real `GroqSTTProvider` against a deterministic local compatible endpoint.

## #42 canonical Live transcript UI — integrated

The web transcript surface preserves these semantics:

- canonical HTTP cursor reads are truth;
- WebSocket delivery is an ephemeral wake hint, not persistence;
- duplicate/out-of-order wake hints converge through canonical reads;
- reconnect/degraded delivery can catch up without changing archive recording safety;
- transcript presentation does not own recorder lifecycle or archive durability.

## #44 resumable existing-recording upload foundation — integrated

The durable transfer path is:

```text
Browser file
  -> Uppy + tus
  -> tusd
  -> shared durable audio storage
  -> Recantor tusd completion hook
  -> PostgreSQL upload completion evidence
```

Current transfer properties:

- WAV/MP3/M4A/OGG/WebM/MP4 source admission;
- pause/retry/reload/reselection resumes the same tus upload when recovery evidence matches;
- capability-token protected upload-session reads/hooks;
- durable completion records internal storage identity, exact byte length, and SHA-256;
- whole-file completion hashing runs outside the asyncio event loop and outside a long upload-row lock, then revalidates authoritative binding before publish;
- UI may claim **durably uploaded** only after Recantor confirms durable completion.

## #45 uploaded-media processing — integrated

After immutable #44 completion, the backend path is:

```text
durable completed upload
  -> PostgreSQL UploadMediaProcessing
  -> bounded ffprobe
  -> bounded FFmpeg normalize to mono signed 16-bit PCM / 16 kHz
  -> claim-fenced atomic first-wins normalized evidence
  -> deterministic upload-energy-vad-180s-v1 segmentation
  -> existing commit_utterance_work
  -> existing STTJob / STTProvider
  -> canonical TranscriptSegment
```

Important properties:

- one PostgreSQL processing identity per immutable completed upload;
- PostgreSQL remains durable authority; media Redis/Celery messages are wake mechanisms only;
- dedicated `media-upload` worker has RW audio access, while live/upload STT workers keep audio storage RO;
- normalized publication does heavy digest/fsync work while private, then performs the bounded final first-wins install under the current PostgreSQL claim fence;
- stale/reclaimed workers cannot publish authoritative normalized identity;
- deterministic D3-A retry starts from normalized sample 0 and reproduces utterance/timeline identity or fails loudly;
- partial EOF timing preserves coverage with floor(start)/ceil(end);
- long silence creates no fake utterance work;
- valid blank provider text becomes terminal `no_speech`, not failure and not a blank `TranscriptSegment`;
- migration 0009 backfills completed #44 uploads exactly once and has a behavioral downgrade/re-upgrade proof.

Residual alpha boundaries remain documented: the atomic first-wins filesystem proof targets the Linux/local-filesystem Compose deployment shape; a crash before manifest commit can leave a non-authoritative orphan content object; out-of-band storage mutation is detected rather than repaired automatically.

## #43 product shell — integrated

The desktop productization pass provides:

- obvious `Live` / `Upload recording` top-level workflow shell;
- active microphone `requesting` or `recording` capture cannot be hidden behind Upload navigation;
- Live prioritizes lifecycle + elapsed time, one primary action, audio safety, transcription state, recovery/loss controls, then canonical transcript;
- archive-audio and transcription state are visually/semantically separate;
- fenced ownership, missing-audio loss, spool failure, recovery, and sync controls remain reachable outside Diagnostics;
- `Advanced / Diagnostics` is collapsed by default and read-only;
- #42 transcript logic remains canonical/reconnect truth;
- normal setup truth is centered on server-side `GROQ_API_KEY`;
- laptop layout/accessibility/focus and requesting-navigation safety have Chromium regression coverage.

The Upload screen now includes the independently reviewed #46 processing/result surface. It preserves the existing #44 capability boundary, renders canonical transcript results in recording-timeline order, and exposes TXT/JSON/VTT/SRT as derived views rather than a second transcript store.

## Configuration truth

Backend `Settings` currently implements direct Groq configuration plus durable scheduler/media defaults.

Normal setup centers on:

- `GROQ_API_KEY`.

Implemented advanced defaults include:

- Live STT queue `stt-live`;
- Upload STT queue `stt-upload`;
- media queue `media-upload`;
- bounded STT/media claim, retry, reconciliation, probe, normalize, and subprocess-output settings.

`STT_PRIMARY_PROVIDER`, `STT_FALLBACK_PROVIDER`, and `LOCAL_STT_BASE_URL` are **not** implemented settings and must not appear as normal setup selectors.

Docker Compose forwards `GROQ_API_KEY` to `api`, `stt-worker`, and `stt-upload-worker`. The web and media-worker services do not need the Groq secret. Real credentials remain local and uncommitted.

## Current acceptance gate — #48

Issue #48 is the bounded cloud/fresh-install gate before one final Windows Chrome/Edge campaign. Its existing proof matrix remains authoritative; the focused implementation lane is DRAFT PR #57 on `agent-m/issue-48-alpha-proof`.

The remaining #48 proof is composition-only:

- secretless successful Live Compose STT through the existing `GroqSTTProvider` pointed at a deterministic local compatible endpoint, including one Redis/worker/reconciler recovery case;
- representative supported media exercised with ffprobe/FFmpeg inside the actual final `media-worker` image/runtime;
- whole-stack durable state surviving `docker compose down` without `-v` and restart, followed by `down -v` and a true zero-state fresh boot;
- a bounded PowerShell witness helper for the later Windows campaign with exact-SHA/clean-tree, stack-health, redacted evidence, post-run, and secret-safety assertions.

This #48 lane does not reopen #44/#45/#46 accepted semantics and does not perform the Windows witness itself. #47 remains optional and outside this gate.

## Not implemented yet

The following must not be described as working current capabilities:

- local faster-whisper fallback/provider selection;
- diarization/speaker labels;
- rolling/final summaries;
- production authentication/authorization;
- user-visible retention/deletion policy UX;
- production reverse-proxy/deployment hardening;
- native Android/iOS recorder clients.

Optional Issue #47 / PR #50 remains parked and is not required for the first installable alpha.

## Production exposure boundary

The current alpha remains trusted-development software. Authentication, authorization, retention/deletion behavior, abuse controls, and production deployment hardening are required before exposing sensitive recordings/transcripts to untrusted users.

## Immediate coordination rule

1. Complete the bounded #48 A/B/C/D cloud proof on DRAFT PR #57 without reopening accepted predecessor lanes unless a concrete regression appears.
2. Independently review/integrate only the exact accepted #48 candidate if the proof is green.
3. If canonical integration changes, obtain exact resulting-head checks before any Windows authorization.
4. Only after the cloud gate passes, run one bounded Windows Chrome/Edge campaign with the approved exact candidate.
5. Do not merge PR #53 / integration to `main` without explicit Bohan authorization.
