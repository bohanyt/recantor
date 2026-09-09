# Roadmap

This roadmap sequences capability by dependency and reliability. It is not a promise that every phase ships unchanged; it is the current delivery order until replaced by repository evidence and ADRs.

## Phase 0 — Foundation

Goal: establish a stable source of truth before application code expands.

Deliverables:

- product contract;
- architecture contract;
- stack ADR;
- agent working contract;
- initial repository/tooling scaffold;
- basic CI and test commands;
- development Docker Compose baseline.

Exit condition:

A new contributor/agent can understand the product boundary, run the project locally, and know what "reliable recording" means without relying on chat history.

## Phase 1 — Reliable web recording

Goal: prove the durability-critical path on desktop Chrome/Edge.

Deliverables:

- session create/resume/finalize API;
- responsive web recorder UI;
- browser local spool via IndexedDB/Dexie;
- numbered/idempotent chunk upload;
- durable server ACK;
- PostgreSQL session/chunk metadata;
- filesystem `AudioStorage` implementation;
- heartbeats and interruption state;
- reconnect and missing-sequence reconciliation;
- explicit gap reporting;
- basic session history/status.

Validation scenarios:

- two or more simultaneous browser sessions;
- temporary Wi-Fi/network loss then recovery;
- refresh and resume;
- duplicate chunk retry;
- API restart after acknowledged chunks;
- browser tab close and later recovery/finalization;
- long desktop recording with Chrome/Edge in the background/minimized while the computer remains awake.

Exit condition:

Recantor can make a bounded, evidence-backed claim that acknowledged desktop-browser audio survives ordinary network/app interruptions.

## Phase 2 — Live transcription

Goal: add low-latency transcript without weakening Phase 1.

Deliverables:

- utterance/VAD pipeline;
- Groq STT provider;
- canonical transcript segment model;
- live STT queue;
- provider retry/rate-limit handling;
- WebSocket transcript updates;
- transcript recovery after reconnect;
- local STT provider contract;
- benchmark harness for latency/accuracy/provider behavior.

Exit condition:

Two concurrent recordings can produce isolated live transcripts while capture remains safe during provider degradation.

## Phase 3 — Existing recording upload

Goal: transcribe arbitrary existing recordings reliably.

Deliverables:

- guest/upload session flow;
- Uppy + tus/tusd resumable upload;
- expiration and capability-token access;
- upload size/duration/rate limits;
- FFmpeg normalization;
- queued transcription;
- TXT/JSON/VTT/SRT exports.

Exit condition:

A large interrupted upload can resume without restarting and produce a final transcript through the same canonical transcript model.

## Phase 4 — Local GPU fallback

Goal: keep transcription available when external STT is unavailable or constrained.

Deliverables:

- faster-whisper/CTranslate2 service;
- health/capacity API;
- warm model workers;
- one worker capacity lane per configured GPU initially;
- STT router/fallback policy;
- provider latency/error instrumentation;
- benchmark on target deployment hardware.

Exit condition:

Groq failure can degrade to local transcription without changing recording/session semantics.

## Phase 5 — Speaker diarization

Goal: attribute transcript segments to stable speakers.

Deliverables:

- diarization provider boundary;
- live/provisional speaker labels;
- final/offline reconciliation pass;
- transcript-speaker alignment;
- UX for provisional vs final speaker state;
- accuracy/latency benchmarks on real meeting audio.

Exit condition:

A multi-speaker meeting can produce stable generic speaker labels with known limitations documented.

## Phase 6 — Rolling meeting intelligence

Goal: turn transcript evidence into useful live meeting state.

Deliverables:

- `SummaryProvider` boundary;
- structured meeting-state schema;
- rolling updates based on transcript ranges;
- key points;
- decisions;
- action items and owners where evidenced;
- open questions;
- current topic;
- final full-context summary;
- queue isolation so LLM delay cannot affect capture/STT.

Exit condition:

Meeting intelligence updates incrementally, can be reconciled at finalization, and never becomes a source of truth over the transcript.

## Phase 7 — Native Android/iOS persistent recorder

Goal: provide reliable phone recording for lock-screen/background/offline use where platform APIs permit it.

Deliverables:

- shared documented session/chunk protocol;
- native durable local file/spool;
- background recording lifecycle;
- offline capture;
- persistent upload queue;
- OS audio interruption handling;
- resume/reconciliation UX;
- same server-side sessions/transcript/intelligence pipeline as web.

Exit condition:

Phone users can intentionally choose the native recorder when they need stronger lifecycle guarantees than a mobile browser can provide.

## Phase 8 — Speaker identification and final polish

Goal: convert generic speaker labels to known names when explicitly enrolled and sufficiently confident.

Deliverables:

- opt-in enrollment;
- speaker embeddings/voiceprint storage policy;
- similarity matching and calibrated threshold;
- confidence/fallback UX;
- deletion/retention controls;
- final transcript reconciliation;
- improved final summary/export quality.

Exit condition:

Known-speaker naming works as an optional convenience feature without being treated as authentication or overriding uncertain evidence.

## Phase 9 — Organization/product maturity

Possible future capabilities:

- organization accounts and roles;
- SSO/OIDC;
- configurable retention policies;
- S3-compatible/object storage;
- semantic transcript search;
- meeting templates;
- calendar integration;
- follow-up generation;
- webhooks/public API;
- MCP/agent integrations;
- observability dashboards;
- backup/restore tooling;
- downstream branding/reskin system;
- optional desktop native recorder/system-audio capture.

These are downstream of a proven reliable capture/transcript core.

## Sequencing rule

Do not pull a later feature into the critical path just because it is interesting.

In particular:

- summaries do not gate transcription;
- transcription does not gate recording durability;
- diarization does not gate canonical raw transcript storage;
- speaker identification does not gate diarization;
- native mobile apps do not require a second backend.
