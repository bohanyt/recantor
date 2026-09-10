# Current

Last updated: 2026-09-10

This file is the short operational source of truth for Recantor. Inspect GitHub fresh before acting; repository, PR, issue, branch, and CI state outrank this summary if the repository has moved.

## Product truth

Recantor is a public, self-hosted recording and meeting-intelligence project with two intended web workflows:

1. **Live Intelligence** — authenticated live recording, transcript, speaker processing, and rolling meeting intelligence.
2. **Transcribe Recording** — upload an existing recording or use a simple browser recorder, then process/export it. A bounded guest path may operate without login.

Desktop Chrome/Edge on an **awake** computer is the first recording reliability target. Mobile web may work while actively executing, but Recantor does not promise continuous browser capture through screen lock, OS suspension, sleep, or shutdown.

> Capture is infrastructure. Intelligence is downstream.

The archive recording path must remain independent from STT, diarization, realtime delivery, and LLM availability.

## Durable archive architecture — MERGED / Phase 1 CLOSED

```text
MediaRecorder
  -> atomic Dexie/IndexedDB fragment + local high-water commit
  -> sequenced HTTP upload with hash/timing/ownership evidence
  -> crash-safe filesystem audio commit + PostgreSQL acceptance metadata
  -> durable HTTP ACK
  -> local fragment deletion
```

Important invariants:

- IndexedDB is a recovery spool, not final durability;
- an ACK requires durable audio plus durable acceptance metadata;
- one live session has one active capture generation fenced by writer identity + epoch;
- raw MediaRecorder chunks are ordered media fragments and are not assumed independently decodable;
- Stop/finalize declares a final sequence boundary;
- every expected sequence through that boundary must be durable or explicitly represented as loss before completion;
- liveness interruption is not automatically audio loss;
- PostgreSQL/audio storage are durable truth; Redis/WebSocket are not.

Phase 1 Issue #5 is closed. The bounded real-platform Case K witness ran on Windows 11 25H2 build `26200.9168`, Microsoft Edge `152.0.4191.66` / Chromium `152.0.7977.83`, built-in laptop microphone, local Compose, and an awake desktop. The whole Edge window was minimized for about five minutes; the final session reached `COMPLETE`, ACK sequence `179`, pending local audio `0`, and explicit gaps `0`.

## Terminal audio completeness — MERGED

Issue #15 / PR #29 was squash-merged as `ba5c91145d70334335f06e8447f9310d8dac165b`; post-merge CI `34437634299` succeeded.

`state == complete` means the declared final sequence boundary is fully accounted for. Downstream consumers must also inspect persisted `audio_completeness`:

- `full` — non-zero final boundary and every expected sequence is durably present;
- `partial` — non-zero final boundary with one or more explicit sequence-loss gaps;
- `empty` — final sequence boundary is zero and no audio sequence was captured.

Retrying an already-complete finalize preserves the persisted classification.

## Canonical transcript foundation — MERGED

Issue #30 / PR #31 was squash-merged as `1aba22e22b859e84dfb016bc2bd211e067467e9c`; post-merge CI `34439502585` succeeded.

Canonical transcript truth is PostgreSQL-backed immutable `TranscriptSegment` evidence with:

- stable segment ID and `session_id`;
- per-session monotonically increasing transcript `sequence` used as reconnect/publication cursor;
- opaque producer key for retry idempotency;
- explicit `start_ms` / `end_ms` recording timeline;
- text plus optional language;
- uniqueness on `(session_id, sequence)` and `(session_id, producer_key)`;
- reconnect-safe HTTP read via `/api/v1/sessions/{session_id}/transcript?after_sequence=...`.

Transcript sequence is not recording-chunk sequence and is not an implicit timestamp.

## Durable transcription utterance work — MERGED

Issue #32 / PR #33 was squash-merged to `main` as `9ea3ee7ac9c8e9e1093ee78477ae1dc7f0d09939`. The merge established the provider-neutral speech-sized audio-work boundary future STT workers consume.

A committed `TranscriptionUtterance` has:

- deterministic work UUID derived from `(session_id, canonical producer_key)`;
- per-session monotonically increasing utterance sequence;
- explicit `start_ms` / `end_ms`;
- normalized media content type, SHA-256, byte length, and internal storage key;
- independently decodable durable media in `sessions/<session-id>/utterances/<work-uuid>.media`;
- a durable identity manifest used to preserve conflict/idempotency semantics across a crash before DB commit;
- deterministic future transcript producer key `utterance:<work-uuid>`.

The archive ingest/ACK path does not call this boundary. Archive fragments and transcription utterance work are different evidence namespaces.

## Current Phase 2 slice — Issue #34 / PR #35

Issue #34 defines Phase 2C: an independent realtime PCM/VAD utterance producer on top of the durable work contract. Draft PR #35 is on branch `phase2/realtime-utterance-producer`.

Merge candidate head validated by the real-platform witness before this docs reconciliation:

`b2210436f2321ddfde4ef033428ab06b3069dc4d`

Automated CI for that exact head: run `34447070869` — **SUCCESS** across backend, frontend, Chromium E2E, and Compose smoke.

Candidate implementation:

```text
same microphone MediaStream
        |
        +---------------- archive lane ----------------+
        |                                               |
        |  MediaRecorder -> IndexedDB -> HTTP -> durable ACK
        |
        +--------------- realtime lane ----------------+
           AudioWorklet -> ~20 ms sample-clock PCM
                         -> bounded WebSocket transport
                         -> server energy VAD / endpointing
                         -> mono PCM WAV
                         -> durable TranscriptionUtterance
```

Realtime rules:

- reuse the already-acquired microphone stream; no second `getUserMedia`;
- PCM frames carry monotonic sample offsets, not browser-timer-derived timing;
- realtime backpressure/discontinuity may degrade live intelligence but must not block or weaken archive capture;
- a sample-offset discontinuity resets uncommitted VAD state rather than fabricating continuity;
- server handshake and every durable utterance commit are fenced by active writer/capture epoch;
- utterance output is independently decodable mono PCM WAV and is committed through the #33 durable work boundary;
- realtime graph cleanup does not own/stop the shared microphone tracks;
- operator UI exposes realtime `inactive / connecting / live / degraded` plus committed-utterance count separately from archive durability.

### Real Windows Edge microphone witness — PASS

Witness environment:

- exact candidate head `b2210436f2321ddfde4ef033428ab06b3069dc4d`;
- Windows 11 25H2 build `26200.9168`;
- Microsoft Edge `152.0.4191.66`, Chromium `152.0.7977.83`;
- ordinary Edge Default profile;
- built-in laptop microphone;
- local Docker Compose at `http://localhost:5173`;
- PC remained awake while the whole Edge window was minimized/backgrounded during the bounded run.

Foreground checkpoint at `00:01:10`:

- archive `Emitted audio synced`;
- pending local audio `0`;
- ACK through sequence `34`;
- explicit gaps `0`;
- realtime `live`;
- durable utterances `15`.

Final checkpoint after restore and normal Stop at `00:07:09`:

- `COMPLETE`;
- `Server synced`;
- pending local audio `0`;
- ACK through sequence `211`;
- explicit gaps `0`;
- realtime `inactive` after Stop as expected;
- durable utterances `71`.

Server-side witness session `181b5dd8-41c8-49d8-86ea-f2e00ec74ae1`:

- `state=complete`, `audio_completeness=full`;
- `final_sequence=211`, `final_monotonic_end_ms=429737`;
- 211 recording chunks;
- 71 transcription utterance rows with sequence `1..71`;
- timeline bounds `first_start_ms=1`, `last_end_ms=427761`;
- all 71 rows are `audio/wav`;
- filesystem contains 71 `.media` objects and 71 `.json` identity manifests;
- sample `.media` object decodes as mono 16-bit PCM WAV at 48 kHz with 72,000 frames.

Issue #34 contains the witness record. Do not generalize this evidence to sleep, lock suspension, or mobile background execution.

`docs/ARCHITECTURE.md` was reviewed against the candidate and already describes the required dual-lane archive/realtime ownership model, durable utterance namespace, and failure-degradation invariant; no architecture-contract rewrite is required for this witness.

PR #35 should remain unmerged until explicitly authorized. After the docs commit has its own green CI, it may be marked review-ready.

## Exact next dependency after Phase 2C

After #35 is merged and post-merge CI is green, the next bounded slice may wire an owned `STTProvider`/worker to committed utterance work and write canonical transcript segments using `utterance:<work-uuid>` producer keys.

Before treating current VAD thresholds as product-quality defaults, run real-office speech/noise/latency tuning. Current energy thresholds, minimum voiced duration, silence endpoint, pre-roll, and hard maximum are explicit tuning defaults, not permanent guarantees.

Groq is the intended primary live STT provider; local faster-whisper/CTranslate2 remains a planned fallback where deployment resources permit it. Provider failure must retain durable utterance work and must never participate in archive ACK semantics.

## Production exposure boundary

Authentication, authorization, retention/deletion, abuse controls, and production deployment hardening remain required before exposing Live Intelligence as a production service. Trusted local development may continue before that exposure boundary is complete.

## Not implemented yet

- Groq or local STT provider execution;
- Celery live STT processing;
- canonical transcript generation from utterance work;
- realtime transcript fanout/UI/reconnect client;
- diarization/speaker labels;
- rolling/final summaries;
- production authentication/authorization;
- user-visible recording deletion/retention;
- tus existing-recording upload pipeline;
- production reverse proxy/deployment hardening;
- native Android/iOS recorder clients.

Do not describe these as working until repository evidence proves them.

## Known boundaries

- Browser close, desktop sleep/shutdown, execution-suspending lock behavior, and mobile OS suspension cannot be treated as continuous capture.
- Browser local storage remains best-effort unless persistence is granted.
- An open MediaRecorder fragment is not yet durable browser/server evidence; abrupt process/device loss can lose an un-emitted bounded tail.
- Fenced old-generation evidence remains a recovery/retention policy problem.
- Cross-device takeover/backlog semantics are not claimed.
- Transcript revision/supersession semantics remain deferred.
- Speaker/diarization semantics are absent from the first canonical transcript row.
- Realtime discontinuity is live-intelligence evidence loss, not automatically an archive gap.

## Local development

```bash
git clone https://github.com/bohanyt/recantor.git
cd recantor
docker compose -f infra/compose.yaml up --build
```

Open exactly `http://localhost:5173`; API is `http://localhost:8000`.

`apps/api/tests/conftest.py` drops the entire test schema on teardown; never point pytest at a database used by a running API instance.

## Handoff rule

For a new Control Tower chat:

1. inspect GitHub fresh;
2. read `AGENTS.md`;
3. read this file;
4. read the latest dated file under `docs/handoff/` if present;
5. read Issue #34 and PR #35 (or their successors) plus current CI evidence;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
