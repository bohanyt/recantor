# Product

## What Recantor is

Recantor is a self-hosted recording and meeting-intelligence platform. Its job is to reliably preserve audio, turn it into a canonical transcript, identify speakers where possible, and derive useful meeting state such as summaries, decisions, action items, and open questions.

The product is intentionally split between **capture** and **intelligence**. Capture must remain trustworthy even when downstream AI services are slow or unavailable.

## Primary workflows

### 1. Live Intelligence

Authenticated users can start a live recording from the web application.

Primary first-production target:

- Chrome and Edge on laptops/desktops

Responsive active-page support:

- Android browsers
- iOS Safari/browser environments

Expected live experience:

- clear recording state and elapsed time;
- audio continuity status;
- live transcript updates;
- provisional speaker labels;
- rolling meeting summary;
- decisions, action items, open questions, and current topic;
- explicit degraded/interrupted states;
- Stop triggers finalization rather than merely stopping the UI.

The live summary may lag behind the transcript. That is acceptable. Summary latency must never be allowed to block recording.

A trusted development build may prove recording reliability before authentication exists, but the Live Intelligence workflow is not production-ready until session ownership and authorization are implemented.

### 2. Transcribe Recording

A user can provide an existing audio/video recording and receive a transcript and exports.

The guest path may operate without login, but it must still use a private unguessable session/capability token, bounded retention, upload limits, and abuse controls.

Expected inputs include common formats such as:

- WAV
- MP3
- M4A
- OGG
- WebM
- MP4 audio tracks

The server normalizes supported media with FFmpeg before STT when needed.

Large or unstable uploads should use resumable upload infrastructure rather than a custom partial-upload implementation.

### 3. Browser Record for guest/simple use

The upload page may also expose a simple Record button.

Browser recording is useful when the page remains active, but the product must not claim that mobile browser background or lock-screen capture is guaranteed.

For reliable persistent phone recording, the roadmap includes native Android and iOS recorder clients.

## Reliability model

### Recording success is independent from AI success

A meeting can be considered successfully captured even if:

- Groq is unavailable;
- local STT is unavailable;
- diarization is delayed;
- an LLM provider is rate limited;
- live transcript delivery is temporarily disconnected.

Those failures create delayed processing or degraded intelligence, not lost audio.

### Browser live recording

The intended browser durability model is:

1. capture audio;
2. persist an encoded chunk in local browser storage;
3. upload with session ID and monotonic sequence number;
4. server durably writes the chunk and acceptance metadata;
5. server acknowledges the sequence;
6. browser may then delete its local copy.

If the network disappears, unacknowledged chunks remain local and are retried while browser capture/execution remains alive.

Browser local storage is a recovery spool, not the same durability guarantee as a server ACK. Recantor should request persistent origin storage when available, monitor local quota, and visibly report when the local recovery path is no longer safe.

If the browser reconnects, client and server reconcile acknowledged/missing sequences.

Only one active capture writer should own a live session at a time. Multiple tabs/devices must not silently create competing sequence streams for one session.

### Stop and finalization

A clean Stop has a declared final sequence/high-water mark. Recantor should not mark a session complete until every expected sequence through that boundary is either durably present or explicitly represented as a gap/failure.

If a browser disappears without a clean Stop, the session becomes interrupted rather than silently complete.

### Interruptions

A browser closing, crashing, refreshing, losing network, sleeping, or being suspended must not silently transform an incomplete recording into a complete one.

Sessions may enter an interrupted/recovering state. When the user returns, Recantor should offer recovery where possible and show any audio gap it cannot recover.

### Mobile web boundary

Mobile browser recording is supported as a convenience path while the page remains usable/active.

Do not promise:

- uninterrupted recording after screen lock;
- uninterrupted recording while the OS freezes/discards the page;
- uninterrupted recording while another app takes exclusive audio capture.

The product should keep the screen awake when supported and warn users about the limitation.

## Native recorder direction

Future Android and iOS clients should be deliberately thin recording clients of the same server protocol.

They add:

- background capture;
- lock-screen capture where the OS permits it;
- durable local recording files;
- offline capture;
- persistent upload queue;
- recovery from application/network interruptions.

They should not introduce a separate transcript model or independent meeting backend.

## Access, privacy, and retention

Meeting audio and transcripts are sensitive data.

For authenticated Live Intelligence:

- sessions/audio/transcripts belong to an authenticated owner or authorized scope;
- unguessable URLs are not an authorization model;
- users need a visible way to delete recordings they own;
- retention behavior must be explicit before production exposure.

For guest workflows:

- access uses high-entropy capability tokens;
- tokens and results expire according to bounded retention;
- upload/processing limits and abuse controls are required.

Recantor is not designed for covert recording. The UI must make active recording obvious to the operator. Deployers/operators remain responsible for applicable participant notice and consent requirements in their jurisdiction and organization.

Organization-wide roles, SSO policy, compliance retention rules, and similar enterprise policy can mature later without changing the capture model.

## Canonical outputs

The server maintains canonical structured transcript state. Export files are derived views.

Expected export formats:

- TXT
- JSON/JSONL
- VTT
- SRT

A human-readable TXT form should resemble:

```text
[00:00:03] Speaker A: Selamat pagi semuanya.
[00:00:08] Speaker B: Kita mulai dari jadwal implementasi.
```

## Speaker handling

### Diarization

Diarization answers: **who spoke when?**

Live results may initially use stable generic labels such as `Speaker A`, `Speaker B`, and so on.

### Speaker identification

A later authenticated feature may match diarized speakers against explicitly enrolled voice embeddings and display names when confidence is sufficient.

Requirements:

- opt-in enrollment;
- secure storage;
- deletion/retention controls;
- confidence threshold calibrated on real deployment audio;
- fallback to generic speaker labels when confidence is insufficient;
- never use speaker identification as authentication.

## Meeting intelligence

The live summary should be stateful rather than repeatedly regenerating the entire meeting from scratch.

A conceptual meeting state is:

```json
{
  "current_topic": "Implementation schedule",
  "key_points": ["Target before the 20th"],
  "decisions": [],
  "action_items": [
    {"text": "Confirm vendor schedule", "owner": null}
  ],
  "open_questions": ["Who owns the vendor relationship?"]
}
```

Later transcript evidence may amend earlier state. For example, if a participant later accepts an action item, update the owner rather than creating a duplicate.

The canonical transcript remains the source of evidence. Meeting intelligence is derived state.

## Finalization

Stopping a live meeting starts a finalization process.

Finalization may include:

- reconciling the declared final sequence/high-water mark;
- waiting for all expected chunks or explicitly recording gaps;
- processing queued/untranscribed audio;
- reconstructing/normalizing final audio;
- reconciling transcript ordering;
- higher-quality/offline diarization;
- optional speaker identification;
- final full-context summary;
- export generation.

A session should not be marked complete before required durable/finalization work is reconciled or explicitly marked failed.

## Concurrent use

Multiple simultaneous sessions are a normal product condition.

Requirements:

- strict session isolation;
- one active capture writer per individual live session;
- bounded concurrency;
- fair scheduling across meetings;
- separate queue priorities for latency-sensitive and deferrable work;
- one noisy/long meeting must not starve all others;
- provider rate limits must create backpressure, not audio loss.

## Product states

The exact implementation may evolve, but the product should be able to represent states equivalent to:

```text
CREATED
RECORDING / UPLOADING
INTERRUPTED / RECOVERING
PROCESSING
FINALIZING
COMPLETE
FAILED
```

Subsystem degradation should be represented separately when possible. For example, a session can still be recording safely while live STT is degraded.

## Public upstream scope

The public Recantor repository should remain reusable and brand-neutral.

Deployment-specific concerns such as company branding, internal domains, identity-provider settings, retention policy, and infrastructure addresses belong in configuration or a downstream deployment repository.

## Non-goals for the initial product

The first implementation does not need:

- native mobile clients before the web path works;
- meeting bots that automatically join Zoom/Meet/Teams;
- Kubernetes;
- Kafka;
- semantic enterprise search;
- biometric authentication;
- perfect speaker names in live mode;
- guaranteed mobile browser recording under screen lock.

These may become future product work only when the core capture and transcript paths are proven reliable.
