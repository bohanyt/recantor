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

Canonical transcript truth is PostgreSQL-backed immutable `TranscriptSegment` evidence with stable segment/session identity, per-session monotonically increasing transcript sequence, opaque producer key, explicit `start_ms` / `end_ms`, text plus optional language, uniqueness on `(session_id, sequence)` and `(session_id, producer_key)`, and reconnect-safe HTTP reads.

Transcript sequence is independent from recording-chunk and utterance-work sequence.

## Durable transcription utterance work — MERGED

Issue #32 / PR #33 was squash-merged as `9ea3ee7ac9c8e9e1093ee78477ae1dc7f0d09939`.

A committed `TranscriptionUtterance` has deterministic work identity, per-session sequence, explicit timing, normalized media metadata, independently decodable durable media, a durable identity manifest, and deterministic canonical transcript producer key `utterance:<work-uuid>`.

Archive fragments and transcription utterances are distinct evidence namespaces. Archive ingest/ACK does not call the utterance path.

## Realtime PCM/VAD utterance producer — MERGED / Phase 2C CLOSED

Issue #34 / PR #35 was squash-merged to `main` as `7e0b2601ca17148db28df4b422d93c48273780cc`. Post-merge CI run `34450139207` succeeded across backend, frontend, Chromium E2E, and Compose smoke.

Merged realtime architecture:

```text
same microphone MediaStream
        |
        +---------------- archive lane ----------------+
        |  MediaRecorder -> IndexedDB -> HTTP -> durable ACK
        |
        +--------------- realtime lane ----------------+
           AudioWorklet -> ~20 ms sample-clock PCM
                         -> bounded WebSocket transport
                         -> server energy VAD / endpointing
                         -> mono PCM WAV
                         -> durable TranscriptionUtterance
```

The realtime lane reuses the acquired microphone, carries sample-clock offsets, may degrade independently under backpressure/discontinuity, is fenced by active writer/capture epoch, emits independently decodable WAV utterances, and never participates in archive ACK semantics.

### Real Windows Edge witness — PASS

The Phase 2C witness used session `181b5dd8-41c8-49d8-86ea-f2e00ec74ae1` on Windows 11 / Edge with the real built-in microphone. After a bounded whole-window background/minimize interval and normal Stop, archive state was `COMPLETE` / `full`, final sequence `211`, pending `0`, explicit gaps `0`, and 71 durable utterance rows/files were present. Sample utterance media decoded as mono 16-bit PCM WAV at 48 kHz.

Current VAD thresholds are tuning defaults, not product-quality guarantees.

## Current Phase 2 slice — Issue #36 / PR #37

Issue #36 is the active Phase 2D dependency: **STT provider boundary and durable utterance-to-transcript execution**.

Current candidate PR #37 is `phase2/stt-provider-executor`. Implementation head before this documentation reconciliation was:

`a0af7124f6389a4800010feb7ef62dc1b514f335`

Exact-head CI run `34454490245` succeeded across backend, frontend, Chromium E2E, and Compose smoke.

Candidate causal path:

```text
durable TranscriptionUtterance
        -> verified storage read
        -> owned STTProvider
        -> Groq whisper-large-v3-turbo
        -> normalized result
        -> canonical TranscriptSegment
           producer_key = utterance:<work-uuid>
```

Candidate rules:

- STT consumes only committed durable utterance work, never arbitrary raw MediaRecorder chunks;
- durable manifest/path/hash/length evidence is verified before provider execution;
- provider execution stays fully downstream from archive capture/ACK/finalization;
- canonical timing is copied from the utterance work item;
- retry identity is the existing `utterance:<work-uuid>` producer key;
- already-committed canonical work short-circuits provider execution on sequential retry;
- blank/malformed/provider failure creates no fake transcript evidence and leaves durable utterance work intact;
- Groq API key is server-side only and Compose forwards local environment configuration without committing secrets;
- Groq transport uses an explicit non-browser API `User-Agent` because the default Python `urllib` signature was rejected by Groq's Cloudflare edge with Error 1010.

### Real Groq provider witness — PASS

The provider boundary has now been exercised twice against the previously validated real-microphone session.

First work item:

- utterance `87aec8c6-f11a-50b5-84d8-160d448eec1c`, sequence 29;
- timing `152321..155621 ms`, duration 3300 ms, 316844 bytes, `audio/wav`;
- initial request with default `urllib` client signature reached `api.groq.com` but Cloudflare returned HTTP 403 Error 1010 `browser_signature_banned`;
- the same durable work succeeded when the request used an explicit non-browser API `User-Agent`;
- Groq `whisper-large-v3-turbo` returned non-empty Indonesian text;
- Recantor committed canonical segment `845ce524-0b1d-4b66-b700-45ff2d2221ed`, transcript sequence 1, preserving `152321..155621 ms`, `language=id`, `idempotent=False`.

PR #37 then made that `User-Agent` behavior permanent and added a regression assertion.

Second work item, using the permanent adapter with no runtime monkeypatch:

- utterance `ad71e495-cdd6-5ae0-814b-758ba5133f85`, sequence 39;
- timing `225261..228101 ms`, duration 2840 ms, 272684 bytes, `audio/wav`;
- Groq call succeeded directly from the rebuilt PR #37 API container;
- canonical segment `6117f3e8-fae4-4c49-a3e5-9f78d91a7357`, transcript sequence 2, preserved `225261..228101 ms`, `language=id`, `idempotent=False`;
- PostgreSQL contained exactly the two expected canonical `utterance:<work-uuid>` rows from these witnesses.

The API key remained local/server-side and was not committed or posted. This proves provider-path mechanics, not transcript accuracy: the sampled utterances did not have recorded ground-truth text, so accuracy remains a later benchmark concern.

## Exit / next dependency

If the docs-reconciled PR #37 head remains CI-green and review finds no new blocker, #37 is ready for merge authorization. Merging #37 should close Issue #36.

The next Phase 2 dependency after #37 is **durable live STT queue/reconciliation/fairness** so newly committed utterances are automatically discovered and processed without making Redis/Celery the source of truth. Realtime transcript fanout/reconnect UI follows after that.

## Roadmap alignment / drift guard

The repository is still following `docs/ROADMAP.md` dependency order. Phase 2 calls for utterance/VAD pipeline, Groq provider, canonical transcript model, live STT queue, retry/rate-limit handling, realtime transcript updates/recovery, local provider contract, and benchmark evidence.

Already landed or proven in the current candidate chain:

- canonical transcript model/reconnect read foundation;
- durable transcription utterance work;
- realtime PCM/VAD utterance producer;
- provider-neutral STT boundary + real Groq utterance-to-canonical-transcript path in PR #37.

Still later Phase 2 work:

- automatic durable live queue/reconciliation/fairness;
- realtime transcript delivery/reconnect UI;
- local faster-whisper provider/fallback;
- provider/VAD latency and accuracy benchmark/tuning.

Do not pull diarization, summaries, native mobile, existing-file upload, or production exposure concerns into the Phase 2 critical path unless a concrete dependency forces it.

## Production exposure boundary

Authentication, authorization, retention/deletion, abuse controls, and production deployment hardening remain required before exposing Live Intelligence beyond trusted development. They do not block trusted local Phase 2 engineering.

## Not implemented yet

- merged automatic/live Groq STT execution from each new utterance;
- durable live STT queue/reconciliation/fairness;
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
- STT accuracy has not yet been benchmarked against known ground-truth meeting speech.

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
5. read Issue #36 and PR #37 (or successors);
6. re-check current branch/PR/issue/CI state before acting.

Whenever a change materially alters current product/architecture truth, update this file in the same delivery.
