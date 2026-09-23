# Current

Last updated: 2026-09-21

This file is the short operational source of truth for Recantor. Fresh GitHub state outranks this summary if a branch, PR, issue, or CI run has moved.

## Active integration line

Canonical cloud-alpha integration branch:

`integration/cloud-alpha-2026-09-11`

The first installable alpha product candidate was accepted on Windows at exact product head:

`b6602a138fc2186fc37b09d09277871f256bceda`

That accepted product head contains the independently reviewed and integrated #42/#43/#44/#45/#46 work, the #48 cloud proof/witness helper, the Windows Docker build transport correction from PR #59, and the truthful alpha footer correction from PR #60.

Issue #48 is CLOSED / completed. Windows acceptance proved:

- documented Windows Docker build/start;
- exact-SHA health/readiness preflight;
- real microphone -> automatic durable STT -> visible canonical transcript -> healthy Stop/finalization;
- archive safety with zero pending local fragments and zero explicit gaps;
- native file-picker Upload -> durable completion -> server-side processing -> canonical transcript;
- TXT/JSON/VTT/SRT exports;
- truthful alpha capability copy at normal desktop viewport.

Manual clicking of the Upload Pause button is not an alpha acceptance requirement. The durable resumability contract is already proven by #44 persisted non-zero tus offset/restart evidence. Current alpha recovery after reopen restores saved upload state and may require reselecting the same local file; true browser-reopen automatic resume is tracked in #61.

Post-acceptance housekeeping reconciles the `main`-only agent-context compiler commit `59ff57502a9a1d84ad332cab106c46c256482d23` into the integration lineage without changing accepted product behavior.

PR #53 remains the integration checkpoint vehicle. No merge to `main` is authorized unless Bohan explicitly says to merge.

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

## Alpha acceptance status — COMPLETE

Issue #48 is complete.

The accepted Windows product witness was performed on exact head:

`b6602a138fc2186fc37b09d09277871f256bceda`

Accepted local evidence includes Windows Docker build/start, final exact-SHA Preflight, Live real-microphone transcription and healthy Stop/finalization, Upload processing into canonical transcript, all four TXT/JSON/VTT/SRT exports, and truthful UI capability wording.

Cloud evidence remains authoritative for deterministic restart/recovery invariants that do not require a human Windows operator, including persisted non-zero tus offset recovery across API/tusd restart and same-upload continuation.

The current alpha is still trusted-development software, not a stable or production-ready release.

Before Recantor may be described as stable:

- #62 / 58A cloud-built release artifacts are accepted and integrated;
- #63 / 58B must still provide the supported updater / previous-known-good / rollback / migration-safety lifecycle;
- #61 must provide product-quality browser-reopen upload recovery, preferring automatic resume through a persisted file handle where browser permissions allow it and falling back honestly when they do not.

#47 remains optional and is not a dependency of the first installable alpha.

## Stable-release engineering — #58 / #62 / #63

The first installable alpha remains frozen at `v0.1.0-alpha.1`.

Issue #62 / 58A is **accepted and integrated**. The supported release-artifact path is now cloud-built rather than client-built:

- GitHub Actions builds the Recantor API and Web release images;
- normal release Compose uses `image:` references and contains no Recantor application `build:` directives;
- one API image is reused by migrate/API/STT/media worker and reconciler roles;
- the Web release image serves prebuilt static assets rather than running Vite development mode;
- exact source revision, channel, schema head, platform, and immutable API/Web digests are recorded in a validated release manifest;
- development/source Compose remains available for contributors.

The first real publication proof is immutable prerelease tag `v0.1.0-alpha.2`, bound to reviewed source commit `a82a7c76b1344803ec1f27e890aecb8898d33989`.

Published proof images:

- `ghcr.io/bohanyt/recantor-api@sha256:fe56a055597d0c559857469a89c721f2ba5c343da8d620dce9bcd936c0065b44`;
- `ghcr.io/bohanyt/recantor-web@sha256:686f145c4336625438c2c7069d15aaaeaa2316c65abcdf9609ef9441516b4ce6`.

Release workflow `35573744267` pulled those exact registry digest references back, verified OCI identity, started them through the pull-only release Compose stack, passed API health/readiness + Web shell smoke, rendered the exact release manifest, and created a GitHub **prerelease**. This is release-path proof, not a stable-product claim.

Issue #63 / 58B now has an implementation candidate in DRAFT PR #65. It is **not accepted yet**; independent review and a bounded Windows updater witness still remain.

The candidate introduces:

- `scripts/recantor.ps1` as the Windows updater entrypoint;
- local updater state under `.recantor/` with current, previous-known-good, pending, channel, and last-attempt truth;
- exact immutable GHCR digest activation only;
- release manifest format 2 with explicit `application_rollback_safe_to_versions` and `backup_required` policy while retaining read compatibility for the already-published format-1 `v0.1.0-alpha.2`;
- forward-only normal `update`; intentional downgrade is a rollback operation;
- candidate migration before application activation;
- health/readiness/Web gating before a candidate becomes known-good;
- application-only rollback to exact previous digests when explicitly declared schema-safe;
- fail-closed `manual_recovery_required` state when rollback is unsafe or rollback itself fails;
- no automatic database downgrade and no volume-deleting recovery path;
- release bundle packaging with updater, pull-only Compose, `.env.example`, and exact release manifest.

The first independent review found the updater core safety semantics coherent but required stronger acceptance evidence before a Windows witness. The bounded correction closes those proof gaps.

Current correction source/proof head `06e0b81cf2483babadf886edaa19b878ec84674a` is green:

- Updater CI `35584723718` SUCCESS:
  - Windows PowerShell unit/contract tests PASS, including backup-required pre-apply gating, rollback-attempt health failure -> `rollback_failed_manual_recovery`, and manual rollback target health failure truth;
  - real Docker install from accepted immutable `v0.1.0-alpha.2` artifacts PASS;
  - N, N+1, rollback-safe-fail, and rollback-unsafe-fail use four distinct API digests and four distinct Web digests;
  - a controlled test-only interruption persists `pending.phase=pulling`, then a fresh updater process retries the **same candidate identity** to successful N+1 completion;
  - N -> N+1 transition proves current/previous manifests carry different exact image identities;
  - rollback-safe failure proves `active-images.env` and the running API/Web containers return to the exact previous N+1 digest references;
  - rollback-unsafe failure is refused with `manual_recovery_required` while current known-good identity remains N+1;
  - representative PostgreSQL/audio data survives all paths;
  - updater stdout/stderr are captured and scanned against a runtime-generated proof secret, and updater state is scanned for both the secret value and `GROQ_API_KEY`; no leak is found.
- Release images `35584723674` SUCCESS:
  - release image build/smoke remains green;
  - candidate manifest v2 validates;
  - release bundle creation remains green.
- normal CI `35584723690` SUCCESS.

These are correction proofs only. Independent correction rereview and the final bounded Windows updater witness still remain before #63 acceptance.

Initial release proof targets `linux/amd64`, matching the accepted Windows Docker Desktop runtime, while leaving multi-arch extension for later work.

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

1. Keep PR #53 DRAFT and do not merge the integration line to `main` without explicit Bohan authorization.
2. Treat #62 / 58A as complete after its accepted cloud build/publish proof; do not reopen it unless a concrete release-artifact regression appears.
3. Active stable-path engineering is now:
   - #63 / 58B — DRAFT PR #65 is the updater/previous-known-good/rollback candidate; source review and final bounded Windows updater witness remain before acceptance;
   - #61 — automatic Upload recovery after browser reopen with a persisted file handle where supported.
4. #47 remains optional/parked and must not block #63 or #61.
5. Do not describe Recantor as stable until the remaining stable-path gates are accepted.
