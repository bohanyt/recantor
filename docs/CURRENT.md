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

Phase 1 Issue #5 is closed. Its bounded real-platform witness ran on Windows 11 25H2 build `26200.9168`, Microsoft Edge `152.0.4191.66` / Chromium `152.0.7977.83`, built-in laptop microphone, local Compose, and an awake desktop. The whole Edge window was minimized for about five minutes; final state was `COMPLETE`, ACK sequence `179`, pending local audio `0`, explicit gaps `0`.

Do not generalize this to sleep, shutdown, execution-suspending lock behavior, or mobile browser suspension.

## Terminal audio completeness — MERGED

Issue #15 / PR #29 was squash-merged as `ba5c91145d70334335f06e8447f9310d8dac165b`; post-merge CI `34437634299` succeeded.

Persisted `audio_completeness` separates lifecycle completion from downstream audio eligibility:

- `full` — non-zero final boundary and every expected sequence is durably present;
- `partial` — non-zero final boundary with one or more explicit sequence-loss gaps;
- `empty` — final boundary zero and no audio sequence captured.

Retrying an already-complete finalize preserves the persisted classification.

## Canonical transcript foundation — MERGED

Issue #30 / PR #31 was squash-merged as `1aba22e22b859e84dfb016bc2bd211e067467e9c`; post-merge CI `34439502585` succeeded.

Canonical transcript truth is PostgreSQL-backed immutable `TranscriptSegment` evidence with:

- stable segment ID + `session_id`;
- per-session monotonically increasing transcript `sequence` used as reconnect/publication cursor;
- opaque producer key for retry idempotency;
- explicit `start_ms` / `end_ms` on the recording timeline;
- text plus optional language;
- uniqueness on `(session_id, sequence)` and `(session_id, producer_key)`;
- reconnect-safe HTTP read at `/api/v1/sessions/{session_id}/transcript?after_sequence=...`.

Transcript sequence is independent from recording-chunk and utterance-work sequence.

## Durable transcription utterance work — MERGED

Issue #32 / PR #33 was squash-merged as `9ea3ee7ac9c8e9e1093ee78477ae1dc7f0d09939`.

A committed `TranscriptionUtterance` has:

- deterministic work UUID derived from `(session_id, canonical producer_key)`;
- per-session monotonically increasing utterance sequence;
- explicit `start_ms` / `end_ms`;
- normalized media content type, SHA-256, byte length, and internal storage key;
- independently decodable durable media at `sessions/<session-id>/utterances/<work-uuid>.media`;
- durable identity manifest preserving retry/conflict semantics across a crash before DB commit;
- deterministic canonical transcript producer key `utterance:<work-uuid>`.

Archive fragments and transcription utterances are distinct evidence namespaces. Archive ingest/ACK does not call the utterance path.

## Realtime PCM/VAD utterance producer — MERGED / Phase 2C CLOSED

Issue #34 / PR #35 was squash-merged to `main` as:

`7e0b2601ca17148db28df4b422d93c48273780cc`

Post-merge CI run `34450139207` succeeded across backend, frontend, Chromium E2E, and Compose smoke.

Merged realtime architecture:

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

Rules proven by implementation/tests:

- reuse the already-acquired microphone stream; no second `getUserMedia`;
- PCM timing derives from monotonically increasing audio sample offsets rather than browser timers;
- realtime backpressure/discontinuity may degrade live intelligence but must not block or weaken archive capture;
- sample-offset discontinuity resets uncommitted VAD state rather than fabricating continuity;
- handshake and durable live utterance commit are fenced by active writer/capture epoch;
- utterance output is independently decodable mono PCM WAV committed through the durable utterance-work boundary;
- realtime graph cleanup does not own/stop shared microphone tracks;
- UI exposes realtime `inactive / connecting / live / degraded` plus committed-utterance count separately from archive durability.

### Real Windows Edge witness — PASS

Candidate witness ran against `b2210436f2321ddfde4ef033428ab06b3069dc4d` before merge:

- Windows 11 25H2 build `26200.9168`;
- Edge `152.0.4191.66`, Chromium `152.0.7977.83`, ordinary Default profile;
- built-in laptop microphone;
- local Docker Compose at `http://localhost:5173`;
- PC awake while whole Edge window was backgrounded/minimized.

Foreground checkpoint `00:01:10`:

- archive synced, pending `0`, ACK `34`, gaps `0`;
- realtime `live`;
- durable utterances `15`.

Final checkpoint after bounded background interval, restore, and normal Stop at `00:07:09`:

- `COMPLETE`, `Server synced`, pending `0`, ACK `211`, gaps `0`;
- realtime `inactive` after Stop as expected;
- durable utterances `71`.

Server witness session `181b5dd8-41c8-49d8-86ea-f2e00ec74ae1`:

- `state=complete`, `audio_completeness=full`;
- `final_sequence=211`, 211 recording chunks;
- 71 utterance rows with sequence `1..71`;
- timeline bounds `1..427761 ms`;
- all 71 rows `audio/wav`;
- 71 durable `.media` objects + 71 `.json` manifests;
- sampled media decodes as mono 16-bit PCM WAV at 48 kHz.

Issue #34 contains the witness and post-merge reconciliation comment. Current VAD thresholds are tuning defaults, not product-quality guarantees.

## Current Phase 2 slice — Issue #36 / Phase 2D

Issue #36 is now the active bounded dependency:

**STT provider boundary and durable utterance-to-transcript execution.**

The missing causal link is exactly:

```text
durable TranscriptionUtterance
        -> owned STTProvider
        -> normalized STT result
        -> canonical TranscriptSegment
           producer_key = utterance:<work-uuid>
```

Scope for #36:

- owned provider-neutral `STTProvider` contract;
- bounded executor for one committed utterance;
- verified read of durable utterance media through the storage boundary;
- first Groq Whisper adapter using server-side configuration only;
- canonical transcript commit with utterance timing preserved;
- retry/idempotency semantics through the existing transcript producer key;
- blank/malformed/provider-failure behavior that leaves durable utterance evidence intact;
- no real provider secret/network dependency in ordinary CI.

Current intended Groq default is `whisper-large-v3-turbo`, configurable. Provider failure remains fully downstream from archive capture.

Explicitly **not** in this slice:

- Celery live scheduling/fairness;
- realtime transcript WebSocket fanout/UI;
- local faster-whisper execution/fallback;
- diarization;
- LLM summaries;
- production auth/authorization;
- existing-file upload.

After #36 proves the one-utterance STT causal link, the next Phase 2 dependency is durable live queue/reconciliation/fairness, followed by realtime transcript delivery/reconnect UI and provider/fallback benchmarking.

## Roadmap alignment / drift guard

The repository is still following `docs/ROADMAP.md` dependency order. Phase 2 calls for: utterance/VAD pipeline, Groq provider, canonical transcript model, live STT queue, retry/rate-limit handling, realtime transcript updates/recovery, local provider contract, and benchmark evidence.

Already landed from that Phase 2 list:

- canonical transcript model/reconnect read foundation;
- durable transcription utterance work;
- realtime PCM/VAD utterance producer.

Current #36 starts the Groq/provider + utterance-to-transcript portion. Queueing, realtime transcript delivery, local fallback, and benchmark/tuning remain later Phase 2 work.

Do not pull diarization, summaries, native mobile, upload flow, or production exposure concerns into the Phase 2 critical path unless a concrete dependency forces it.

## Production exposure boundary

Authentication, authorization, retention/deletion, abuse controls, and production deployment hardening remain required before exposing Live Intelligence beyond trusted development. They do not block trusted local Phase 2 engineering.

## Not implemented yet

- actual merged Groq/local STT execution;
- Celery live STT processing;
- canonical transcript generation from real provider output;
- realtime transcript fanout/UI;
- local faster-whisper fallback;
- diarization/speaker labels;
- rolling/final summaries;
- production auth/authorization;
- user-visible recording deletion/retention;
- tus existing-recording upload pipeline;
- production reverse-proxy/deployment hardening;
- native Android/iOS recorder clients.

Do not describe these as working until repository evidence proves them.

## Known boundaries

- Browser close, desktop sleep/shutdown, execution-suspending lock behavior, and mobile OS suspension cannot be treated as continuous capture.
- Browser local storage remains best-effort unless persistence is granted.
- An open MediaRecorder fragment is not yet durable browser/server evidence; abrupt process/device loss can lose an un-emitted bounded tail.
- Fenced old-generation evidence remains a recovery/retention policy problem.
- Cross-device takeover/backlog semantics are not claimed.
- Transcript revision/supersession semantics remain deferred.
- Speaker/diarization semantics are absent from the canonical raw transcript foundation.
- Realtime discontinuity is live-intelligence evidence loss, not automatically an archive gap.
- VAD defaults need real-office speech/noise/latency tuning before being treated as product-quality.

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
5. read Issue #36 and its current implementation PR if one exists;
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
